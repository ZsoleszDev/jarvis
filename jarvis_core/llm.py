"""Ollama REST API kliens a helyi nyelvi modellhez."""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, TypeVar

import requests

from .config import LlmCfg
from .ollama_log import user_setting
from .textutil import strip_think

T = TypeVar("T")

# Efölött a modellt teljesen a videókártyán lévőnek tekintjük (az /api/ps bájtban számol).
# Már egyetlen réteg (a modell kb. 1,5%-a) a processzoron is kb. 20%-kal lassítja a választ.
FULL_GPU = 0.99
# Ha a modell a beállított kontextussal nem fér el, ezekkel a méretekkel próbálkozunk.
CONTEXT_STEPS = (16384, 12288, 8192)
# Kisebb kontextusra csak akkor váltunk, ha a modell úgy teljesen a videókártyára kerül, vagy legalább
# ennyivel (5 százalékponttal) több jut rá. Különben marad a nagyobb: a böngészésnél (pl. egy hirdetés
# feltöltésénél) a kis kontextusban a modell elveszíti a fonalat, és az pár százalék sebességnél többet ér.
MEANINGFUL_GAIN = 0.05
# A megjegyzett betöltési döntés ennyi ideig érvényes; utána újra kipróbálja (hátha azóta több a hely).
FIT_MEMORY_DAYS = 7
# Az Ollama felhasználói beállításai, amelyek a modell és a kontextus memóriaigényét változtatják: ha
# valamelyik megváltozik, a megjegyzett betöltési döntés már nem érvényes (újra kipróbálja a méreteket).
FIT_SETTINGS = ("OLLAMA_KV_CACHE_TYPE", "OLLAMA_FLASH_ATTENTION", "LLAMA_ARG_FIT_TARGET")
# A képről szóló válasz felső határa (token): pár mondat elég, egy beragadt válasz se foglalja a modellt.
VISION_MAX_TOKENS = 400

# Az Ollama a modellt külön folyamatban futtatja (llama-server, runner). Ha az futás közben leáll (pl. az
# Ollama-hiba miatt: std::bad_alloc), a kérés ezek valamelyikével tér vissza; a következő kérésnél az
# Ollama magától újraindítja, ezért egyszer érdemes újrapróbálni.
RUNNER_CRASH_RE = re.compile(
    r"model runner has unexpectedly stopped|runner process (has )?terminated|llama-server (response|terminated)|"
    r"error reading llama-server|wsarecv|forcibly closed|kényszerítetten bezárta|connection (was )?reset|"
    r"post predict|\bEOF\b", re.IGNORECASE)
RESTART_PAUSE = 1.0   # ennyit vár az újrapróbálás előtt (az Ollama közben észreveszi, hogy leállt)


def is_runner_crash(error: BaseException | str) -> bool:
    """Az Ollama modellfolyamata futás közben leállt-e (és nem pl. maga az Ollama nem fut)."""
    return bool(RUNNER_CRASH_RE.search(str(error)))


def gpu_share(status: dict[str, Any] | None) -> float | None:
    """A modell mekkora része van a videókártyákon (0–1) az /api/ps adatai szerint."""
    size = (status or {}).get("size") or 0
    if not size:
        return None
    return min(1.0, ((status or {}).get("size_vram") or 0) / size)


class OllamaError(RuntimeError):
    """A felhasználónak is megjeleníthető Ollama-hiba."""


class Cancelled(Exception):
    """A felhasználó leállította a folyamatban lévő kérést (Állj gomb, Ctrl+C)."""


def run_interruptible(fn: Callable[[], T], poll: float = 0.1,
                      cancel: threading.Event | None = None,
                      external: threading.Event | None = None) -> T:
    """Háttérszálon futtatja a hívást, hogy a megszakítás azonnal hasson.

    Windows-on a blokkoló hálózati hívás nem szakítható meg Ctrl+C-vel, ezért a hívó szál
    csak rövid időközönként várakozik. Ctrl+C-re, vagy ha az `external` esemény (Állj gomb)
    beáll, a `cancel` eseményt is beállítja, hogy a háttérszál abbahagyja a munkát.
    """
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - továbbadjuk a hívónak
            box["error"] = exc

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    try:
        while worker.is_alive():
            worker.join(poll)
            if external is not None and external.is_set():
                if cancel is not None:
                    cancel.set()
                raise Cancelled()
    except KeyboardInterrupt:
        if cancel is not None:
            cancel.set()
        raise
    if "error" in box:
        raise box["error"]
    return box["value"]


def _with_tag(name: str) -> str:
    return name if ":" in name else name + ":latest"


def parse_json_object(text: str) -> dict[str, Any]:
    """A modell JSON-válasza objektumként (a ```json keretet és a körítést is elviseli)."""
    text = strip_think(text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        try:
            value = json.loads(text[start:end + 1]) if 0 <= start < end else None
        except json.JSONDecodeError:
            value = None
    if not isinstance(value, dict):
        raise OllamaError("A modell nem adott érvényes választ (JSON). Próbáld újra.")
    return value


def normalize_message(msg: dict[str, Any]) -> dict[str, Any]:
    """Egységes alakra hozza a modell válaszát (eszközhívások argumentuma mindig dict)."""
    out: dict[str, Any] = {"role": "assistant", "content": msg.get("content") or ""}
    calls = []
    for call in msg.get("tool_calls") or []:
        fn = call.get("function") or {}
        args = fn.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except json.JSONDecodeError:
                args = {}
        if not isinstance(args, dict):
            args = {}
        calls.append({"function": {"name": fn.get("name", ""), "arguments": args}})
    if calls:
        out["tool_calls"] = calls
    if msg.get("thinking"):
        out["thinking"] = msg["thinking"]
    return out


class OllamaClient:
    def __init__(self, cfg: LlmCfg):
        self.cfg = cfg
        self.base_url = cfg.host.rstrip("/")
        self._session = requests.Session()
        self._session.trust_env = False  # helyi szerver: ne menjen rendszerproxyn át
        self._think_supported = True
        self.last_stats: dict[str, Any] = {}
        # A ténylegesen használt kontextus. Betöltéskor csökkenhet (load_fitted), és utána minden
        # kérésnek ezt kell küldenie, különben az Ollama újratöltené a modellt.
        self.num_ctx = int(cfg.num_ctx or 0)
        # Ha beáll (Állj gomb), a folyamatban lévő kérés Cancelled kivétellel megszakad.
        self.stop_event: threading.Event | None = None
        # Ha az Ollama modellfolyamata leáll, és újrapróbáljuk: ezen szól a felületnek.
        self.on_runner_restart: Callable[[str], None] | None = None
        # A betöltés eredményének emléke (load_fitted): így legközelebb egyből jó kontextussal tölt be.
        self.fit_memory: Path | None = None
        self.server_version = ""
        # Ha beáll (pl. készenlétben), a kérések ennyi ideig tartják a modellt a videókártyán (a beállítás helyett).
        # Függvény is lehet (pl. amíg az automata kereskedés fut, tovább marad betöltve).
        self.keep_alive_override: int | str | Callable[[], int | str] | None = None

    @property
    def keep_alive(self) -> int | str:
        value = self.keep_alive_override
        if value is None:
            return self.cfg.keep_alive
        return value() if callable(value) else value

    # --- alacsony szint -------------------------------------------------

    def _request(self, method: str, path: str, payload: dict | None = None,
                 timeout: float | None = None) -> dict[str, Any]:
        timeout = timeout or self.cfg.request_timeout
        try:
            resp = self._session.request(method, self.base_url + path, json=payload, timeout=timeout)
        except requests.ConnectionError as exc:
            raise OllamaError(
                f"Nem érem el az Ollamát ({self.base_url}). Fut az Ollama? "
                "(Indítsd el a Start menüből, vagy: ollama serve)"
            ) from exc
        except requests.Timeout as exc:
            raise OllamaError(f"Az Ollama nem válaszolt {timeout:.0f} másodperc alatt.") from exc

        if resp.status_code >= 400:
            try:
                detail = resp.json().get("error", resp.text)
            except ValueError:
                detail = resp.text
            raise OllamaError(f"Ollama-hiba ({resp.status_code}): {detail}")
        try:
            return resp.json()
        except ValueError as exc:
            raise OllamaError("Érvénytelen (nem JSON) válasz érkezett az Ollamától.") from exc

    # --- szerver és modell ------------------------------------------------

    def version(self) -> str:
        return str(self._request("GET", "/api/version", timeout=5).get("version", "?"))

    def list_models(self) -> list[str]:
        data = self._request("GET", "/api/tags", timeout=10)
        return [m.get("name") or m.get("model") or "" for m in data.get("models", [])]

    def has_model(self) -> bool:
        want = _with_tag(self.cfg.model)
        return any(_with_tag(name) == want for name in self.list_models())

    def ensure_ready(self) -> str:
        """Ellenőrzi, hogy fut-e az Ollama és le van-e töltve a modell. Visszaadja a verziót."""
        ver = self.server_version = self.version()
        if not self.has_model():
            raise OllamaError(
                f"A(z) '{self.cfg.model}' modell nincs letöltve. Töltsd le: ollama pull {self.cfg.model}"
            )
        return ver

    def load(self) -> None:
        """Betölti a modellt a videókártyára (üres prompttal), hogy az első válasz is gyors legyen."""
        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "prompt": "",
            "stream": False,
            "keep_alive": self.keep_alive,
        }
        if self.num_ctx:
            payload["options"] = {"num_ctx": self.num_ctx}
        timeout = max(300.0, float(self.cfg.request_timeout))
        run_interruptible(lambda: self._request("POST", "/api/generate", payload, timeout=timeout))

    def context_candidates(self) -> list[int]:
        """A kipróbálandó kontextusméretek: a beállított, majd egyre kisebbek, de min_ctx alá nem."""
        start = int(self.cfg.num_ctx or 0)
        floor = int(self.cfg.min_ctx or 0)
        if not start or not floor or floor >= start:
            return [start]
        smaller = {size for size in CONTEXT_STEPS if floor <= size < start} | {floor}
        return [start] + sorted(smaller, reverse=True)

    def load_fitted(self, log: Callable[[str], None] | None = None) -> dict[str, Any] | None:
        """Betölti a modellt úgy, hogy lehetőleg teljesen a videókártyákon fusson.

        Ha a beállított kontextussal a modell egy része a processzorra kerül (ettől a válasz lassul),
        kisebb kontextussal próbálja, de min_ctx alá nem megy, és csak akkor marad a kisebbnél, ha úgy a
        modell teljesen a videókártyára kerül, vagy érdemben (MEANINGFUL_GAIN) több jut rá. Ha a kisebb
        kontextus alig segít (pl. az újabb Ollama tartalékot hagy a kártyákon, és a modell egy kis része
        emiatt mindenképp a processzorra kerül), a nagyobbat tartja meg: a böngészéshez az kell.

        A döntést megjegyzi (fit_memory), így legközelebb, például készenlétből felébredve, egyetlen
        betöltéssel megvan. Azt szándékosan nem erőlteti (num_gpu), hogy minden réteg a videókártyára
        kerüljön: ha a memória tényleg nem elég, Windows-on a kártya a rendszermemóriába lóg át.
        Visszaadja a betöltött modell /api/ps adatait (None, ha nem kérdezhető le).
        """
        say = log or (lambda _text: None)
        candidates = self.context_candidates()
        remembered = self._remembered_fit(candidates)
        if remembered is not None:
            ctx, share_then = remembered
            self.num_ctx = ctx
            self.load()
            status = self.loaded_status()
            share = gpu_share(status)
            if share is None or share >= min(FULL_GPU, share_then - 0.02):
                return status
            say("  A videókártyákon most kevesebb a hely, mint legutóbb; újra kipróbálom a kontextusméreteket.")
        status: dict[str, Any] | None = None
        tried: list[tuple[int, float]] = []   # (kontextus, a videókártyán lévő arány)
        for index, ctx in enumerate(candidates):
            previous, self.num_ctx = self.num_ctx, ctx
            try:
                self.load()
            except OllamaError as exc:
                if index == 0:
                    raise
                # Ritka eset: a kisebb kontextussal nem töltődött be. Vissza az előzőhöz, ami működött.
                say(f"  {ctx} tokennel nem sikerült ({exc}), maradok a(z) {previous} tokenesnél.")
                self.num_ctx = previous
                self.load()
                return self.loaded_status()
            status = self.loaded_status()
            share = gpu_share(status)
            if share is None or share >= FULL_GPU:
                self._remember_fit(candidates, ctx, 1.0 if share is None else share)
                return status
            tried.append((ctx, share))
            if index + 1 >= len(candidates) or not self._worth_going_smaller(tried, candidates):
                break
            say(f"  {ctx} tokenes kontextussal a modell kb. {max(1, round((1 - share) * 100))}%-a "
                "a processzorra került, az lassú lenne.")
            say(f"  Újratöltöm kisebb kontextussal ({candidates[index + 1]} token)...")
        best_ctx, best_share = self._choose(tried)
        if best_ctx != self.num_ctx:
            if best_ctx == tried[0][0] and len(tried) > 1:
                say(f"  A kisebb kontextus ezen nem segített érdemben, visszaállok {best_ctx} tokenre.")
            self.num_ctx = best_ctx
            self.load()
            status = self.loaded_status()
        self._remember_fit(candidates, best_ctx, best_share)
        return status

    @staticmethod
    def _choose(tried: list[tuple[int, float]]) -> tuple[int, float]:
        """A kipróbáltak közül: a legnagyobb kontextus, hacsak egy kisebb érdemben többet nem tesz a
        videókártyára (MEANINGFUL_GAIN)."""
        best_ctx, best_share = tried[0]
        for ctx, share in tried[1:]:
            if share >= best_share + MEANINGFUL_GAIN:
                best_ctx, best_share = ctx, share
        return best_ctx, best_share

    @staticmethod
    def _worth_going_smaller(tried: list[tuple[int, float]], candidates: list[int]) -> bool:
        """Megéri-e még kisebb kontextussal próbálkozni. Az első lépés után az eddigi nyereségből
        becsüljük, mennyit hozhat a legkisebb: ha így sem kerülne teljesen a videókártyára, és érdemben
        sem jutna rá több, a további újratöltés csak idő (egy betöltés 10–20 másodperc)."""
        if len(tried) < 2:
            return True
        (ctx0, share0), (ctx1, share1) = tried[0], tried[-1]
        per_token = max(0.0, share1 - share0) / max(1, ctx0 - ctx1)
        best_possible = share0 + per_token * (ctx0 - candidates[-1])
        # egy egész réteg (kb. 1,5%) a kerekítés miatt még átbillenhet
        return best_possible + 0.015 >= min(FULL_GPU, share0 + MEANINGFUL_GAIN)

    def _fit_key(self, candidates: list[int]) -> str:
        key = f"{_with_tag(self.cfg.model)}|{self.server_version}|{','.join(map(str, candidates))}"
        # A KV-gyorsítótár típusa, a flash attention és a tartalék a kártyákon mást enged elférni: ha
        # változtattál rajtuk, újra kipróbálja (a beállítás nélküli kulcs a régivel egyezik).
        settings = [f"{name}={value.strip()}" for name in FIT_SETTINGS
                    if (value := user_setting(name) or "").strip()]
        return key + ("|" + ",".join(settings) if settings else "")

    def _remembered_fit(self, candidates: list[int]) -> tuple[int, float] | None:
        if self.fit_memory is None or len(candidates) < 2:
            return None
        try:
            data = json.loads(self.fit_memory.read_text(encoding="utf-8"))
            entry = data.get(self._fit_key(candidates)) if isinstance(data, dict) else None
            ctx, share, at = int(entry["num_ctx"]), float(entry["share"]), float(entry.get("at") or 0)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return None
        if time.time() - at > FIT_MEMORY_DAYS * 86400:
            return None
        return (ctx, share) if ctx in candidates and 0 < share <= 1 else None

    def _remember_fit(self, candidates: list[int], ctx: int, share: float) -> None:
        if self.fit_memory is None or len(candidates) < 2:
            return
        try:
            data = json.loads(self.fit_memory.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        data[self._fit_key(candidates)] = {"num_ctx": int(ctx), "share": round(float(share), 4), "at": time.time()}
        try:
            self.fit_memory.parent.mkdir(parents=True, exist_ok=True)
            self.fit_memory.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass

    def unload(self) -> None:
        """Kiüríti a modellt a VRAM-ból."""
        self._request("POST", "/api/generate",
                      {"model": self.cfg.model, "keep_alive": 0, "stream": False}, timeout=15)

    def loaded_models(self) -> list[str]:
        """Az Ollamában épp betöltött modellek neve (az /api/ps szerint)."""
        data = self._request("GET", "/api/ps", timeout=5)
        return [str(m.get("name") or m.get("model")) for m in data.get("models", [])
                if m.get("name") or m.get("model")]

    def unload_all(self) -> list[str]:
        """Minden betöltött modellt kiürít (a Szünet gombhoz). Visszaadja, mit ürített ki."""
        try:
            names = self.loaded_models()
        except OllamaError:
            names = []
        if not any(_with_tag(n) == _with_tag(self.cfg.model) for n in names):
            names.append(self.cfg.model)   # ha az /api/ps nem válaszolt, a sajátunkat biztosan
        unloaded = []
        for name in names:
            try:
                self._request("POST", "/api/generate", {"model": name, "keep_alive": 0, "stream": False},
                              timeout=15)
                unloaded.append(name)
            except OllamaError:
                continue
        return unloaded

    def warm(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
             options: dict[str, Any] | None = None) -> None:
        """Előre feldolgoztatja a beszélgetés elejét (rendszerprompt, eszközök) egyetlen tokennel.

        Az Ollama megjegyzi a feldolgozott elejét, így az első igazi kérdésnél már csak a kérdést
        kell feldolgoznia. A kérésnek ugyanúgy kell kinéznie, mint a későbbieknek (modell,
        kontextus, eszközök, gondolkodó mód, beállítások), különben nem lesz közös az eleje.
        """
        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": messages,
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": {**self._options(), **(options or {}), "num_predict": 1},
        }
        if tools:
            payload["tools"] = tools
        if self._think_supported:
            payload["think"] = bool(self.cfg.think)
        self._chat_request(payload)

    # --- beszélgetés --------------------------------------------------------

    def _options(self) -> dict[str, Any]:
        opts: dict[str, Any] = {"temperature": float(self.cfg.temperature)}
        if self.num_ctx:
            opts["num_ctx"] = self.num_ctx
        return opts

    def _chat_request(self, payload: dict[str, Any],
                      on_text: Callable[[str], None] | None = None,
                      cancel: threading.Event | None = None) -> dict[str, Any]:
        """Egy /api/chat kérés. Ha közben az Ollama modellfolyamata leáll, egyszer újrapróbálja: a
        következő kérésnél az Ollama magától újraindítja (ez 20–40 másodperc). Csak akkor, ha a válaszból
        még semmi nem jött meg (különben kétszer hangzana el ugyanaz)."""
        emitted = [False]

        def tracked(text: str) -> None:
            emitted[0] = True
            if on_text is not None:
                on_text(text)

        send = (lambda p: self._stream_request(p, tracked, cancel)) if payload.get("stream") \
            else (lambda p: self._request("POST", "/api/chat", p))
        restarted = False
        while True:
            try:
                return self._send_chat(send, payload)
            except OllamaError as exc:
                stopped = cancel is not None and cancel.is_set()
                if restarted or emitted[0] or stopped or not is_runner_crash(exc):
                    raise
                restarted = True
                if self.on_runner_restart is not None:
                    try:
                        self.on_runner_restart(str(exc))
                    except Exception:  # noqa: BLE001 - a felület hibája ne akadályozza az újrapróbálást
                        pass
                time.sleep(RESTART_PAUSE)

    def _send_chat(self, send: Callable[[dict[str, Any]], dict[str, Any]],
                   payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return send(payload)
        except OllamaError as exc:
            # Ha a modell nem ismeri a gondolkodó módot, a 'think' mező nélkül próbáljuk újra.
            if "think" in payload and "does not support thinking" in str(exc).lower():
                self._think_supported = False
                return send({k: v for k, v in payload.items() if k != "think"})
            raise

    def _stream_request(self, payload: dict[str, Any], on_text: Callable[[str], None] | None,
                        cancel: threading.Event | None) -> dict[str, Any]:
        """Folyamatos válasz: a szövegdarabokat azonnal továbbadja (on_text), a végén összerakja."""
        try:
            resp = self._session.post(self.base_url + "/api/chat", json=payload, stream=True,
                                      timeout=(10, float(self.cfg.request_timeout)))
        except requests.ConnectionError as exc:
            raise OllamaError(
                f"Nem érem el az Ollamát ({self.base_url}). Fut az Ollama? "
                "(Indítsd el a Start menüből, vagy: ollama serve)"
            ) from exc
        except requests.Timeout as exc:
            raise OllamaError("Az Ollama nem válaszolt időben.") from exc

        with resp:
            if resp.status_code >= 400:
                try:
                    detail = resp.json().get("error", resp.text)
                except ValueError:
                    detail = resp.text
                raise OllamaError(f"Ollama-hiba ({resp.status_code}): {detail}")
            content: list[str] = []
            thinking: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            final: dict[str, Any] = {}
            try:
                for line in resp.iter_lines():
                    if cancel is not None and cancel.is_set():
                        break
                    if not line:
                        continue
                    chunk = json.loads(line)
                    if chunk.get("error"):
                        raise OllamaError(f"Ollama-hiba: {chunk['error']}")
                    msg = chunk.get("message") or {}
                    if msg.get("content"):
                        content.append(msg["content"])
                        if on_text is not None:
                            on_text(msg["content"])
                    if msg.get("thinking"):
                        thinking.append(msg["thinking"])
                    if msg.get("tool_calls"):
                        tool_calls.extend(msg["tool_calls"])
                    if chunk.get("done"):
                        final = chunk
                        break
            except requests.RequestException as exc:
                raise OllamaError(f"Megszakadt a kapcsolat az Ollamával ({type(exc).__name__}).") from exc
            except ValueError as exc:
                raise OllamaError("Érvénytelen válasz érkezett az Ollamától.") from exc
        if not final and not (cancel is not None and cancel.is_set()):
            # A válasz vége ("done") nélkül zárult le: a félbemaradt szöveg nem kész válasz (különben pl. egy
            # feltöltés végén a megjelölés helyett egy félbehagyott mondat lenne az eredmény).
            raise OllamaError("A nyelvi modell válasza félbeszakadt: az Ollama a válasz vége előtt lezárta "
                              "a kapcsolatot.")
        final["message"] = {"role": "assistant", "content": "".join(content),
                            "thinking": "".join(thinking), "tool_calls": tool_calls}
        return final

    def chat(self, messages: list[dict[str, Any]],
             tools: list[dict[str, Any]] | None = None,
             on_text: Callable[[str], None] | None = None,
             options: dict[str, Any] | None = None) -> dict[str, Any]:
        """Egy kör a modellel. Visszaadja az asszisztens üzenetét (content + esetleg tool_calls).

        Ha `on_text` meg van adva, a válasz folyamatosan érkezik, és minden szövegdarab
        azonnal megy tovább (pl. a felolvasónak), még mielőtt a modell befejezné.
        `options`: a beszélgetés saját mintavétele és válaszhossza (pl. temperature, num_predict); a
        kontextusméret mindig a betöltötté, így a modellt nem kell újratölteni.
        A `last_stats` "done_reason" mezője "length", ha a válasz a num_predict határnál maradt abba.
        """
        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": messages,
            "stream": on_text is not None,
            "keep_alive": self.keep_alive,
            "options": {**self._options(), **(options or {})},
        }
        if tools:
            payload["tools"] = tools
        if self._think_supported:
            payload["think"] = bool(self.cfg.think)

        cancel = threading.Event()

        def guarded(text: str) -> None:
            if not cancel.is_set() and on_text is not None:
                on_text(text)

        data = run_interruptible(
            lambda: self._chat_request(payload, guarded if on_text else None, cancel),
            cancel=cancel, external=self.stop_event)
        self.last_stats = {
            key: data.get(key)
            for key in ("total_duration", "load_duration", "prompt_eval_count",
                        "eval_count", "eval_duration", "done_reason")
        }
        return normalize_message(data.get("message") or {})

    def chat_json(self, messages: list[dict[str, Any]], schema: dict[str, Any] | None = None,
                  temperature: float = 0.3, max_tokens: int | None = None) -> dict[str, Any]:
        """Egy kérdés, amire a modell JSON-objektummal válaszol (az Ollama a sémához köti).

        `max_tokens`: a válasz felső határa (num_predict). Ha a válasz eléri, a félbemaradt JSON-t nem lehet
        értelmezni, és OllamaError jön; így egy beragadt válasz sem foglalja percekig a modellt."""
        options: dict[str, Any] = {**self._options(), "temperature": float(temperature)}
        if max_tokens:
            options["num_predict"] = int(max_tokens)
        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": messages,
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": options,
            "format": schema or "json",
        }
        if self._think_supported:
            payload["think"] = False
        try:
            data = run_interruptible(lambda: self._chat_request(payload), external=self.stop_event)
        except OllamaError as exc:
            if schema is None or "format" not in str(exc).lower():
                raise
            payload["format"] = "json"   # régebbi Ollama: séma nélkül, csak "json"
            data = run_interruptible(lambda: self._chat_request(payload), external=self.stop_event)
        self.last_stats = {key: data.get(key) for key in ("total_duration", "prompt_eval_count",
                                                          "eval_count", "eval_duration")}
        return parse_json_object((data.get("message") or {}).get("content") or "")

    def complete(self, messages: list[dict[str, Any]], *, options: dict[str, Any] | None = None,
                 on_text: Callable[[str], None] | None = None, stop_event: threading.Event | None = None,
                 json_schema: dict[str, Any] | None = None) -> dict[str, Any]:
        """Szabad szöveges válasz eszközök és gondolkodó mód nélkül, saját mintavételi beállításokkal (a
        Kreatív menühöz). A kontextusméret a betöltöttel egyezik, így az Ollama nem tölti újra a modellt.

        A szöveg darabonként érkezik az `on_text`-re. Ha a `stop_event` beáll (Állj gomb), a félkész
        szöveggel tér vissza (cancelled=True), nem dob kivételt. A `last_stats`-hoz nem nyúl, hogy a
        közben futó beszélgetés statisztikáját ne írja felül.
        Visszaad: {"content", "done_reason", "cancelled", "eval_count", "eval_duration"}."""
        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": messages,
            "stream": True,
            "keep_alive": self.keep_alive,
            "options": {**self._options(), **(options or {})},
        }
        if json_schema is not None:
            payload["format"] = json_schema
        if self._think_supported:
            payload["think"] = False
        cancel = threading.Event()
        lock = threading.Lock()
        parts: list[str] = []

        def collect(text: str) -> None:
            if cancel.is_set():
                return
            with lock:
                parts.append(text)
            if on_text is not None:
                on_text(text)

        def request() -> dict[str, Any]:
            try:
                return self._chat_request(payload, collect, cancel)
            except OllamaError as exc:
                if json_schema is None or "format" not in str(exc).lower() or parts:
                    raise
                payload["format"] = "json"    # régebbi Ollama: séma nélkül, csak "json"
                return self._chat_request(payload, collect, cancel)

        try:
            data = run_interruptible(request, cancel=cancel, external=stop_event)
        except Cancelled:
            with lock:
                text = "".join(parts)
            return {"content": text, "done_reason": "cancel", "cancelled": True, "eval_count": 0,
                    "eval_duration": 0}
        message = data.get("message") or {}
        return {"content": message.get("content") or "", "done_reason": data.get("done_reason") or "",
                "cancelled": False, "eval_count": data.get("eval_count") or 0,
                "eval_duration": data.get("eval_duration") or 0}

    def vision(self, prompt: str, image_b64: str, conversation: dict[str, Any] | None = None) -> str:
        """Kérdés egy képről.

        `conversation` nélkül külön, egyszeri kérdés (előzmények nélkül). Ha meg van adva (a futó beszélgetés:
        "messages", "tools", "options"), a kép a beszélgetés végére kerül egy új üzenetben, ugyanazzal a
        rendszerprompttal, eszközökkel és beállításokkal: így a modell a feladat ismeretében néz rá, és az Ollama
        gyorsítótárában a beszélgetés eleje megmarad (a következő körben nem kell újra feldolgoznia). A válaszban
        kért eszközhívásokat figyelmen kívül hagyja, csak a szöveg számít; a kép nem kerül az előzmények közé."""
        if conversation is not None:
            messages = [*conversation.get("messages", []), {"role": "user", "content": prompt, "images": [image_b64]}]
            options = {**self._options(), **(conversation.get("options") or {})}
            think = bool(self.cfg.think)
        else:
            messages = [{"role": "user", "content": prompt, "images": [image_b64]}]
            options = {**self._options(), "temperature": 0.2}
            think = False
        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": messages,
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": {**options, "num_predict": VISION_MAX_TOKENS},
        }
        if conversation is not None and conversation.get("tools"):
            payload["tools"] = conversation["tools"]
        if self._think_supported:
            payload["think"] = think
        data = run_interruptible(lambda: self._chat_request(payload), external=self.stop_event)
        content = (data.get("message") or {}).get("content") or ""
        return content.replace("<think>", "").replace("</think>", "").strip()

    def loaded_status(self) -> dict[str, Any] | None:
        """A betöltött modell adatai az /api/ps alapján (méret, VRAM-ban lévő rész, kontextus)."""
        try:
            data = self._request("GET", "/api/ps", timeout=5)
        except OllamaError:
            return None
        want = _with_tag(self.cfg.model)
        for item in data.get("models", []):
            if _with_tag(item.get("name") or item.get("model") or "") == want:
                return item
        return None

    def tokens_per_second(self) -> float | None:
        count = self.last_stats.get("eval_count")
        duration = self.last_stats.get("eval_duration")
        if count and duration:
            return count / (duration / 1e9)
        return None
