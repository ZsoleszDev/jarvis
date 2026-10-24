/**
 * Jarvis API – Angular-szolgáltatás a telefonos apphoz (Angular + Capacitor).
 *
 * Bemásolás: src/app/jarvis/jarvis-api.service.ts. Kell hozzá a HttpClient
 * (provideHttpClient() az app.config.ts-ben, vagy HttpClientModule a régebbi modulos appban).
 *
 * Első indítás:
 *   api.configure('https://gep-neve.tailnet.ts.net');           // a Tailscale-cím (tailscale serve status)
 *   await firstValueFrom(api.pair('123456', 'Pixel 8'));          // a Jarvis ablakában kért kód
 * Utána:
 *   api.chat('Milyen idő lesz holnap?', { wait: 60 }).subscribe(r => console.log(r.reply));
 *   api.events$().subscribe(e => ...);                            // élő napló (hosszú lekérdezés)
 *
 * A végpontok leírása: docs/telefon-api.md.
 */
import { Injectable } from '@angular/core';
import { HttpClient, HttpHeaders, HttpParams } from '@angular/common/http';
import { Observable, firstValueFrom, map } from 'rxjs';

// --- adatok -----------------------------------------------------------------------------------

export type Mode = 'chat' | 'work';
export type JarvisState =
  | 'starting' | 'idle' | 'thinking' | 'working' | 'writing' | 'paused' | 'standby' | 'error';
export type ItemStatus = 'vázlat' | 'kérdés' | 'árdöntés' | 'részben fent' | 'fent' | 'eladva';
export type PlatformStatus = 'feltöltve' | 'nincs fent' | 'hiba';
export type Recency = '' | 'nap' | 'hét' | 'hónap' | 'év';

export interface PairResult { ok: boolean; token: string; device_id: string; name: string; }

export interface ConfirmRequest { type?: 'confirm'; id: number; prompt: string; }

export interface SchedulerJob {
  key: string; title: string; every_minutes: number; enabled: boolean;
  on: boolean;          // hamis, ha a gépen (Automatikus munkák) kikapcsoltad
  available: boolean;   // van-e mit néznie (pl. van-e figyelő)
  last_run: number | null; next_run: number | null; result: string; error: string; running: boolean;
}

export interface SchedulerStatus {
  enabled: boolean; blocked: string; free: boolean; snoozed_until: number | null;
  running: string | null; jobs: SchedulerJob[];
}

export interface Status {
  ok: boolean; api: number; name: string; ready: boolean;
  state: { state?: JarvisState; text?: string; detail?: string };
  paused: boolean; standby: boolean; busy: boolean; mode: Mode; model: string; work: boolean;
  confirm: ConfirmRequest | null; scheduler: SchedulerStatus | null; unread: number; last_event: number;
}

export interface ChatResult {
  ok: boolean; request_id: string; done: boolean;
  heard?: string; reply?: string; errors?: string[];
}

export interface JarvisEvent {
  seq: number; at?: number;
  type: 'hello' | 'state' | 'user' | 'assistant' | 'tool' | 'info' | 'warn' | 'error' | 'timing'
    | 'confirm' | 'confirm_done' | 'work_item' | 'work_removed' | 'work_message' | 'notification' | 'request_done'
    | 'gap';
  channel?: Mode; request_id?: string; text?: string; via?: string;
  state?: JarvisState; detail?: string;
  id?: number | string; label?: string; status?: string; summary?: string;
  prompt?: string; approved?: boolean;
  item?: WorkItem; notice?: Notice; conversation?: Conversation;
  [key: string]: unknown;
}

export interface EventsResponse { ok: boolean; events: JarvisEvent[]; last: number; gap: boolean; }

/** A háttérellenőrzés eredménye: fent van, elkeltnek látszik, már nincs fent, vagy nem nyílt meg. */
export type CheckState = 'live' | 'sold' | 'gone' | 'unknown';

export interface PlatformState {
  status: PlatformStatus; at: number; url?: string; note?: string; dossier?: string;
  posted_at?: number; posted_price?: number; posted_title?: string;   // a feltöltéskor
  check?: CheckState; checked_at?: number; check_note?: string;        // a háttérellenőrzés (listings)
  check_dismissed?: 'sold' | 'gone';                                    // „Nem igaz, fent van” (dismissCheck)
}

/** Ahol a fent lévő hirdetésben még a régi ár vagy cím van (csak az eltérő mezők). */
export interface Drift { platform: string; price?: number; title?: string; }

export interface SimilarListing { site: string; text: string; price: number; url: string; auction?: boolean; }

export interface Research {
  query: string; at: number; count: number; kept?: number;
  dropped?: Record<string, number>;   // miért maradt ki: kereső, csomag, alkatrész, saját, más termék, ismétlődő, kevésbé illő
  similar: SimilarListing[]; errors: string[]; reason?: string;
  min?: number; max?: number; p25?: number; median?: number; p75?: number; n?: number;
  confidence?: 'megbízható' | 'bizonytalan'; outliers?: number; suggested?: number;
}

/** A fotóellenőrzés jelzése (modell nélkül, csak jelzés: a feltöltést nem állítja meg). */
export type PhotoWarningKind =
  | 'blurry' | 'dark' | 'bright' | 'clipped' | 'small'   // homályos, sötét, túl világos, ellenfényes, kicsi
  | 'duplicate'                                          // majdnem ugyanaz, mint a tárgy egy másik fotója
  | 'elsewhere';                                         // ugyanez a fotó egy másik tárgynál is megvan
export interface PhotoWarning { kind: PhotoWarningKind; text: string; }
/** A felvételkor mért értékek (a régebbi tárgyaknál indítás után a háttérben készülnek el). */
export interface PhotoCheck {
  v: number; w: number; h: number; sharp: number; mean: number; p05: number; p95: number;
  white: number; black: number; rgb: number[]; hash: string; detail: string;
}

export interface WorkItem {
  id: string; name: string; status: ItemStatus; created: number; updated: number;
  photos: string[]; targets: string[]; prompt: string; research_wanted: boolean;
  product: string; kind: string; title: string; description: string; category: string;
  brand: string; size: string; condition: string; color: string; material: string;
  price: number | null; min_price: number | null; location: string; shipping: string;
  edited: string[]; platforms: Record<string, PlatformState>;
  // confirm: furcsa összeg, amire visszakérdezett („3 Ft-ra gondoltál?”); igen: decidePrice(id, 'confirmed')
  pending: { kind?: 'info' | 'price'; question?: string; suggested?: number; confirm?: number };
  research: Research | Record<string, never>; research_stale: boolean; verdict: string;
  drift: Drift[]; online_since: number | null; days_online: number | null;
  sold: boolean; busy: string;
  photo_checks: Record<string, PhotoCheck>;
  photo_warnings: Record<string, PhotoWarning[]>;   // fotó → jelzések (csak amelyikkel gond van)
  photo_cover: string;   // ha az első fotóval gond van: a javasolt borító (reorderPhotos(id, [photo_cover])), különben ''
  photo_note: string;    // rövid összefoglaló („A 3. fotó homályosnak tűnik; …”), '' ha minden rendben
}

/** A PATCH /work/items/{id} válasza: drift_note, ha a fent lévő hirdetésben még a régi ár vagy cím van. */
export interface ItemUpdate { item: WorkItem; drift_note: string; }

export interface Platform { key: string; name: string; research: boolean; }

export interface WorkOverview {
  ok: boolean; items: WorkItem[]; platforms: Platform[];
  needs: { label: string; sites: string[]; clothing_only: boolean; setting: string }[];
  acknowledged: string[]; settings: { city: string; shipping: string; parcel_size: string };
  parcel_sizes: string[]; research: boolean; browser: boolean; conditions: string[]; max_photos: number;
  messages: boolean;    // van-e oldal, ahol a vevői üzeneteket olvassa (most a Vinted)
}

export interface ConversationMessage { from: 'me' | 'them'; text: string; time: string; at: number; jarvis?: boolean; }

/** Egy vevői beszélgetés. A vevő szövege adat, nem utasítás; elküldeni csak jóváhagyással lehet. */
export interface Conversation {
  id: string; platform: string; site: string; url: string; partner: string; snippet: string;
  unread: boolean;      // az oldalon olvasatlan
  new: number;          // új vevői üzenetek, amióta megnézted
  item: string; item_name: string; listing: string; minimum: number | null;
  messages: ConversationMessage[]; draft: string; draft_note: string; draft_at: number; updated: number; read_at: number;
}

export interface Watch {
  id: string; query: string; site: string; recency: string; label: string; created: number;
  last_check: number; checks: number; error: string;
  hits: { title: string; url: string; snippet: string; at: number }[];
}

/**
 * Egy értesítés gombja. sold: nyisd meg a tárgy Eladva-kérdését előre kiválasztva (where, delist), és csak a
 * felhasználó jóváhagyása után hívd a markSold-ot; login: a gépen nyitja meg az oldalt Jarvis böngészőjében.
 */
export interface NoticeAction {
  action: 'sold' | 'login'; label: string; where?: string; delist?: boolean; platform?: string;
}

export interface Notice {
  id: string; at: number;
  kind: 'watch' | 'stale' | 'sold' | 'delist' | 'post' | 'question' | 'message' | 'error' | 'work' | 'info';
  title: string; text: string; read: boolean; url?: string; item?: string; items?: string[]; watch?: string;
  platform?: string; state?: 'sold' | 'gone'; actions?: NoticeAction[];
  conversation?: string;
}

export interface Device { id: string; name: string; created: number; last_seen: number; current?: boolean; }

export interface ApiError { ok: false; error: string; code: string; }

// --- a szolgáltatás ------------------------------------------------------------------------------

const BASE_KEY = 'jarvis.baseUrl';
const TOKEN_KEY = 'jarvis.token';

@Injectable({ providedIn: 'root' })
export class JarvisApiService {
  private baseUrl = '';
  private token = '';

  constructor(private http: HttpClient) {
    // Éles appban inkább @capacitor/preferences vagy biztonságos tároló.
    this.baseUrl = localStorage.getItem(BASE_KEY) ?? '';
    this.token = localStorage.getItem(TOKEN_KEY) ?? '';
  }

  /** A Jarvis címe, pl. https://gep-neve.tailnet.ts.net (a /api/v1 nélkül). */
  configure(baseUrl: string, token?: string): void {
    this.baseUrl = baseUrl.replace(/\/+$/, '');
    localStorage.setItem(BASE_KEY, this.baseUrl);
    if (token !== undefined) this.setToken(token);
  }

  get paired(): boolean { return !!this.baseUrl && !!this.token; }

  forget(): void { this.setToken(''); }

  private setToken(token: string): void {
    this.token = token;
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  }

  private url(path: string): string { return `${this.baseUrl}/api/v1${path}`; }

  private headers(): HttpHeaders {
    return new HttpHeaders(this.token ? { Authorization: `Bearer ${this.token}` } : {});
  }

  private get<T>(path: string, params?: Record<string, string | number | boolean>): Observable<T> {
    return this.http.get<T>(this.url(path), { headers: this.headers(), params: toParams(params) });
  }

  private post<T>(path: string, body: unknown = {}, params?: Record<string, string | number | boolean>): Observable<T> {
    return this.http.post<T>(this.url(path), body, { headers: this.headers(), params: toParams(params) });
  }

  // --- párosítás és állapot ---

  ping(): Observable<{ ok: boolean; name: string; api: number }> {
    return this.http.get<{ ok: boolean; name: string; api: number }>(this.url('/ping'));
  }

  /** A Jarvis ablakában (Telefon › Új párosító kód) kért 6 jegyű kóddal. A kulcsot el is tárolja. */
  pair(code: string, device: string): Observable<PairResult> {
    return this.http.post<PairResult>(this.url('/pair'), { code, device }).pipe(
      map((result) => { this.setToken(result.token); return result; }));
  }

  status(): Observable<Status> { return this.get<Status>('/status'); }

  // --- beszélgetés ---

  /** wait > 0: megvárja a választ (legfeljebb ennyi mp-ig). */
  chat(text: string, opts: { mode?: Mode; wait?: number; resume?: boolean } = {}): Observable<ChatResult> {
    return this.post<ChatResult>('/chat', { text, mode: opts.mode ?? 'chat', wait: opts.wait ?? 60,
                                            resume: opts.resume ?? false });
  }

  stop(): Observable<unknown> { return this.post('/stop'); }
  /** Szünet: minden modell leáll, a gép videókártyája és memóriája felszabadul (pl. játékhoz). */
  pause(): Observable<unknown> { return this.post('/pause'); }
  /** Folytatás: újra betölti a modelleket (10–20 mp). */
  resume(): Observable<unknown> { return this.post('/resume'); }
  reset(mode: Mode = 'chat'): Observable<unknown> { return this.post('/reset', { mode }); }

  pendingConfirm(): Observable<ConfirmRequest | null> {
    return this.get<{ confirm: ConfirmRequest | null }>('/confirm').pipe(map((r) => r.confirm));
  }

  answerConfirm(id: number, approved: boolean): Observable<unknown> {
    return this.post('/confirm', { id, approved });
  }

  /**
   * Élő napló: hosszú lekérdezéssel követi a Jarvis eseményeit, amíg fel vagy iratkozva.
   * Hiba után egyre ritkábban próbálkozik (1 mp → 30 mp). Ha közben események vesztek el,
   * egy {type: 'gap'} eseményt ad: ilyenkor töltsd újra az állapotot és a tárgyakat.
   */
  events$(after = 0): Observable<JarvisEvent> {
    return new Observable<JarvisEvent>((subscriber) => {
      let stopped = false;
      let delay = 1000;
      let cursor = after;
      const loop = async (): Promise<void> => {
        while (!stopped) {
          try {
            const response = await firstValueFrom(this.get<EventsResponse>('/events', { after: cursor, wait: 25 }));
            delay = 1000;
            if (response.gap) subscriber.next({ seq: response.last, type: 'gap' });
            for (const event of response.events) subscriber.next(event);
            cursor = response.last;
          } catch (error) {
            if (stopped) break;
            if (isUnauthorized(error)) { subscriber.error(error); break; }
            await sleep(delay);
            delay = Math.min(delay * 2, 30000);
          }
        }
      };
      void loop();
      return () => { stopped = true; };
    });
  }

  // --- Munka: Adás/Vétel ---

  work(): Observable<WorkOverview> { return this.get<WorkOverview>('/work'); }

  createItem(targets: string[], prompt: string, researchWanted = false): Observable<WorkItem> {
    return this.post<{ item: WorkItem }>('/work/items', { targets, prompt, research_wanted: researchWanted })
      .pipe(map((r) => r.item));
  }

  item(id: string): Observable<WorkItem> {
    return this.get<{ item: WorkItem }>(`/work/items/${id}`).pipe(map((r) => r.item));
  }

  /** Ha a fent lévő hirdetésben még a régi ár vagy cím van, a drift_note megmondja (mutasd meg). */
  updateItem(id: string, fields: Partial<WorkItem>): Observable<ItemUpdate> {
    return this.http.patch<ItemUpdate>(this.url(`/work/items/${id}`), { fields }, { headers: this.headers() });
  }

  deleteItem(id: string): Observable<unknown> {
    return this.http.delete(this.url(`/work/items/${id}`), { headers: this.headers() });
  }

  /** Egy fotó (pl. a Capacitor Camera képe: fetch(photo.webPath).then(r => r.blob())). */
  uploadPhoto(id: string, photo: Blob): Observable<WorkItem> {
    return this.http.post<{ item: WorkItem }>(this.url(`/work/items/${id}/photos`), photo, {
      headers: this.headers().set('Content-Type', photo.type || 'image/jpeg'),
    }).pipe(map((r) => r.item));
  }

  photoUrl(id: string, name: string, thumb = true): string {
    return `${this.url(`/work/items/${id}/photos/${name}`)}?thumb=${thumb ? 1 : 0}` +
      `&access_token=${encodeURIComponent(this.token)}`;
  }

  deletePhoto(id: string, name: string): Observable<WorkItem> {
    return this.http.delete<{ item: WorkItem }>(this.url(`/work/items/${id}/photos/${name}`), { headers: this.headers() })
      .pipe(map((r) => r.item));
  }

  reorderPhotos(id: string, order: string[]): Observable<WorkItem> {
    return this.post<{ item: WorkItem }>(`/work/items/${id}/photos/order`, { order }).pipe(map((r) => r.item));
  }

  /** Feltöltés a kiválasztott oldalakra (előbb acknowledge, ha 409 'acknowledge' jön). */
  sell(id: string, resume = false): Observable<unknown> { return this.post(`/work/items/${id}/sell`, { resume }); }
  reply(id: string, text: string): Observable<unknown> { return this.post(`/work/items/${id}/reply`, { text }); }
  /** 'confirmed': igen a visszakérdezett összegre (pending.confirm). */
  decidePrice(id: string, choice: 'keep' | 'suggested' | 'confirmed' | number): Observable<unknown> {
    return this.post(`/work/items/${id}/price`, { choice });
  }
  cancelQuestion(id: string): Observable<unknown> { return this.post(`/work/items/${id}/cancel`); }
  postTo(id: string, platform: string): Observable<unknown> { return this.post(`/work/items/${id}/post`, { platform }); }
  research(id: string): Observable<unknown> { return this.post(`/work/items/${id}/research`); }

  /** Eladva: hol kelt el (where), és vegye-e le Jarvis a többi oldalról (delist). */
  markSold(id: string, where = '', delist = true):
      Observable<{ item: WorkItem; still_posted: string[]; delisting: string[] }> {
    return this.post<{ item: WorkItem; still_posted: string[]; delisting: string[] }>(
      `/work/items/${id}/sold`, { where, delist });
  }

  unmarkSold(id: string): Observable<{ item: WorkItem }> {
    return this.post<{ item: WorkItem }>(`/work/items/${id}/sold`, { sold: false });
  }

  delist(id: string, platforms?: string[]): Observable<{ platforms: string[] }> {
    return this.post<{ platforms: string[] }>(`/work/items/${id}/delist`, platforms ? { platforms } : {});
  }

  markPlatform(id: string, platform: string, status: PlatformStatus, url = ''): Observable<{ item: WorkItem }> {
    return this.post<{ item: WorkItem }>(`/work/items/${id}/mark`, { platform, status, url });
  }

  /** „Nem igaz, fent van”: a háttérellenőrzés téves jelzése (elkelt, eltűnt) ezen az oldalon. */
  dismissCheck(id: string, platform: string): Observable<WorkItem> {
    return this.post<{ item: WorkItem }>(`/work/items/${id}/dismiss`, { platform }).pipe(map((r) => r.item));
  }

  acknowledge(platform: string): Observable<{ acknowledged: string[] }> {
    return this.post<{ acknowledged: string[] }>('/work/ack', { platform });
  }

  saveSettings(settings: { city?: string; shipping?: string; parcel_size?: string }): Observable<unknown> {
    return this.http.put(this.url('/work/settings'), { settings }, { headers: this.headers() });
  }

  // --- Munka: vevői üzenetek ---

  messages(): Observable<{ conversations: Conversation[]; unread: number; browser: boolean }> {
    return this.get<{ conversations: Conversation[]; unread: number; browser: boolean }>('/work/messages');
  }

  conversation(id: string): Observable<Conversation> {
    return this.get<{ conversation: Conversation }>(`/work/messages/${id}`).pipe(map((r) => r.conversation));
  }

  /** A (kézzel átírt) válaszvázlat mentése. */
  saveDraft(id: string, draft: string): Observable<Conversation> {
    return this.http.put<{ conversation: Conversation }>(this.url(`/work/messages/${id}/draft`), { draft },
      { headers: this.headers() }).pipe(map((r) => r.conversation));
  }

  /** Válaszvázlat a modellel; a kész vázlat work_message eseményként jön. */
  requestDraft(id: string, resume = false): Observable<unknown> {
    return this.post(`/work/messages/${id}/draft`, { resume });
  }

  markConversationSeen(id: string): Observable<{ conversation: Conversation }> {
    return this.post<{ conversation: Conversation }>(`/work/messages/${id}/seen`);
  }

  /** Küldés: a Jarvis beírja, és a küldés gombja előtt jóváhagyást kér (confirm esemény, answerConfirm). */
  sendMessage(id: string, text: string, resume = false): Observable<unknown> {
    return this.post(`/work/messages/${id}/send`, { text, resume });
  }

  checkMessages(): Observable<unknown> { return this.post('/work/messages/check'); }

  // --- háttérmunka, figyelők, értesítések, eszközök ---

  jobs(): Observable<SchedulerStatus> { return this.get<SchedulerStatus>('/jobs'); }
  runJob(key: 'watches' | 'stale' | 'listings' | 'messages' | string): Observable<unknown> {
    return this.post(`/jobs/${key}/run`);
  }
  /** Ennyi percig nincs háttérmunka (0: újra mehet). */
  snooze(minutes: number): Observable<{ snoozed_until: number | null }> {
    return this.post<{ snoozed_until: number | null }>('/jobs/snooze', { minutes });
  }

  watches(): Observable<Watch[]> { return this.get<{ watches: Watch[] }>('/watches').pipe(map((r) => r.watches)); }
  addWatch(query: string, site = '', recency: Recency = 'hét'): Observable<Watch> {
    return this.post<{ watch: Watch }>('/watches', { query, site, recency }).pipe(map((r) => r.watch));
  }
  removeWatch(id: string): Observable<unknown> {
    return this.http.delete(this.url(`/watches/${id}`), { headers: this.headers() });
  }

  notifications(unreadOnly = false, limit = 50): Observable<{ notifications: Notice[]; unread: number }> {
    return this.get<{ notifications: Notice[]; unread: number }>('/notifications', { unread: unreadOnly ? 1 : 0, limit });
  }
  markRead(ids?: string[]): Observable<{ marked: number }> {
    return this.post<{ marked: number }>('/notifications/read', ids ? { ids } : {});
  }

  devices(): Observable<Device[]> { return this.get<{ devices: Device[] }>('/devices').pipe(map((r) => r.devices)); }
  revokeDevice(id: string): Observable<unknown> {
    return this.http.delete(this.url(`/devices/${id}`), { headers: this.headers() });
  }
}

// --- segédek -----------------------------------------------------------------------------------

function toParams(values?: Record<string, string | number | boolean>): HttpParams | undefined {
  if (!values) return undefined;
  let params = new HttpParams();
  for (const [key, value] of Object.entries(values)) params = params.set(key, String(value));
  return params;
}

function sleep(ms: number): Promise<void> { return new Promise((resolve) => setTimeout(resolve, ms)); }

function isUnauthorized(error: unknown): boolean {
  return typeof error === 'object' && error !== null && (error as { status?: number }).status === 401;
}
