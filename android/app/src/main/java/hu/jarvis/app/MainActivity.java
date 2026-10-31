package hu.jarvis.app;

import android.Manifest;
import android.annotation.TargetApi;
import android.app.Activity;
import android.app.DownloadManager;
import android.app.NotificationManager;
import android.content.ActivityNotFoundException;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.graphics.Bitmap;
import android.graphics.Color;
import android.graphics.ImageDecoder;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.os.PowerManager;
import android.provider.MediaStore;
import android.provider.OpenableColumns;
import android.webkit.JavascriptInterface;
import android.webkit.URLUtil;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.ConnectException;
import java.net.SocketTimeoutException;
import java.net.UnknownHostException;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

import org.json.JSONObject;

/**
 * A Jarvis ablaka a telefonon: a gépen futó Jarvis felülete (ugyanaz, mint a gépen), a Tailscale-en át. Minden a
 * gépen fut; a telefon csak mutatja, és innen adod a feladatokat. Párosítás nélkül a beállító oldal
 * (assets/setup.html) jön; ha a gép nem érhető el, ugyanaz, a hibával.
 *
 * Indítási kérések (a gép felülete a JarvisApp.pending() hívással kérdezi le, és teljesíti):
 * - új hirdetés: parancsikon;
 * - megosztás a Jarvisnak: fotók a galériából (az új hirdetéshez; a felület a /__jarvis_shared/ címről kéri le
 *   őket, ezt a WebView-ban mi szolgáljuk ki), vagy szöveg, link (a beszélgetésbe);
 * - párosító link a gép QR-kódjából (jarvis://pair): kitölti a beállító oldalt, a párosítást te indítod;
 * - egy megérő hirdetés értesítése (NotifyService.dealAlert): a találat ablaka (ár, mennyiért mennek, mennyit
 *   kereshetsz vele, és a gomb, ami kimásolja az eladónak szóló üzenetet a vágólapra és megnyitja a hirdetést);
 * - egy elkeltnek látszó hirdetés értesítésének gombja (NotifyService.soldAction): a hirdetés Eladva ablaka, előre
 *   kiválasztva; a levételt ott, a Rendben gombbal hagyod jóvá.
 */
public class MainActivity extends Activity {
    /** Látszik-e az ablak: ilyenkor a háttérszolgáltatás nem értesít (a felületen úgyis látod). */
    static volatile boolean visible;

    static final String ACTION_NEW_LISTING = "hu.jarvis.app.NEW_LISTING";
    static final String ACTION_SOLD = "hu.jarvis.app.SOLD";
    static final String ACTION_DEAL = "hu.jarvis.app.DEAL";
    static final String ACTION_ITEM = "hu.jarvis.app.ITEM";
    /** A ChatGPT alkalmazás (a Szakértői tipp gombhoz); ha nincs telepítve, a weboldala a böngészőben. */
    static final String CHATGPT_PACKAGE = "com.openai.chatgpt";
    static final String CHATGPT_URL = "https://chatgpt.com/";
    private static final String SETUP = "file:///android_asset/setup.html";
    private static final String SHARED_PATH = "/__jarvis_shared/";
    private static final int MAX_SHARED = 20;
    private static final int PICK_FILES = 1;
    private static final int ASK_NOTIFY = 3;
    private static final int TAKE_PHOTO = 4;
    private static final long SLEEP_AFTER_MS = 60 * 1000;

    private WebView web;
    private Prefs prefs;
    private volatile String currentUrl = "";
    private ValueCallback<Uri[]> filesCallback;
    // A még nem teljesített indítási kérések (a WebView száláról is olvassuk).
    private volatile boolean wantListing;
    private volatile String sharedText = "";
    private volatile List<Uri> sharedPhotos = Collections.emptyList();
    private volatile int sharedBatch;
    private volatile boolean photosNew;
    private volatile String pairServer = "";
    private volatile String pairCode = "";
    private volatile String wantSold = "";
    private volatile String wantDeal = "";
    private volatile String wantItem = "";                 // {"id", "open"}: egy tárgy értesítése (kérdés, Nézd át)
    private Uri cameraShot;                                // a kamerával most készülő fotó helye (PhotoProvider)

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        prefs = new Prefs(this);
        web = new WebView(this);
        web.setBackgroundColor(Color.BLACK);
        setContentView(web);
        WebSettings settings = web.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setMediaPlaybackRequiresUserGesture(false);
        settings.setUserAgentString(settings.getUserAgentString() + " JarvisAndroid/1");
        web.addJavascriptInterface(new Bridge(), "JarvisApp");
        web.setWebViewClient(new Client());
        web.setWebChromeClient(new Chrome());
        web.setDownloadListener((url, agent, disposition, mime, length) -> download(url, disposition, mime));
        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, ASK_NOTIFY);
        }
        // A legutóbbi alkalmazások közül (vagy újraindítás után) az eredeti kérést nem teljesítjük újra.
        boolean fresh = state == null
                && (getIntent().getFlags() & Intent.FLAG_ACTIVITY_LAUNCHED_FROM_HISTORY) == 0;
        if (fresh && take(getIntent())) {
            web.loadUrl(SETUP + "#pair");
        } else {
            open();
        }
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        if (take(intent)) {
            web.loadUrl(SETUP + "#pair");
        } else if (prefs.paired() && currentUrl.startsWith(prefs.server())) {
            web.evaluateJavascript("window.JarvisPending && window.JarvisPending()", null);
        } else {
            open();
        }
    }

    /** Egy indítás kérése (új hirdetés, megosztás, párosító link, értesítés); igaz, ha párosító link jött. */
    private boolean take(Intent intent) {
        String action = intent == null ? null : intent.getAction();
        if (action == null) {
            return false;
        }
        switch (action) {
            case ACTION_NEW_LISTING:
                wantListing = true;
                return false;
            case ACTION_SOLD:
                wantSold = soldRequest(intent);
                return false;
            case ACTION_ITEM: {                           // egy tárgy értesítése: a kérdése, vagy a Nézd át ablak
                int notification = intent.getIntExtra("notification", 0);
                if (notification != 0) {
                    getSystemService(NotificationManager.class).cancel(notification);
                }
                try {
                    wantItem = new JSONObject().put("id", String.valueOf(intent.getStringExtra("item")))
                            .put("open", String.valueOf(intent.getStringExtra("open"))).toString();
                } catch (Exception e) {
                    wantItem = "";
                }
                return false;
            }
            case ACTION_DEAL: {                           // egy megérő hirdetés értesítése: a találat ablaka
                int notification = intent.getIntExtra("notification", 0);
                if (notification != 0) {
                    getSystemService(NotificationManager.class).cancel(notification);
                }
                String deal = intent.getStringExtra("deal");
                wantDeal = deal == null ? "" : deal;
                return false;
            }
            case Intent.ACTION_SEND:
            case Intent.ACTION_SEND_MULTIPLE:
                String type = intent.getType();
                if (type != null && type.startsWith("image/")) {
                    List<Uri> photos = sharedUris(intent);
                    if (!photos.isEmpty()) {
                        sharedPhotos = photos;
                        sharedBatch++;
                        photosNew = true;
                    }
                } else {
                    CharSequence text = intent.getCharSequenceExtra(Intent.EXTRA_TEXT);
                    if (text != null) {
                        sharedText = text.toString();
                    }
                }
                return false;
            case Intent.ACTION_VIEW:
                Uri data = intent.getData();
                if (data == null || !"jarvis".equals(data.getScheme())) {
                    return false;
                }
                pairServer = Api.normalize(data.getQueryParameter("server"));
                String code = data.getQueryParameter("code");
                pairCode = code == null ? "" : code.replaceAll("[^0-9]", "");
                return true;
            default:
                return false;
        }
    }

    /**
     * Az értesítés „Eladva itt…” gombja: melyik tárgy, hol kelt el, és vegye-e le a többi oldalról (a felület ezzel
     * nyitja meg az Eladva ablakot; magától nem jelöl meg és nem vesz le semmit). Az értesítés eltűnik.
     */
    private String soldRequest(Intent intent) {
        int id = intent.getIntExtra("notification", 0);
        if (id != 0) {
            getSystemService(NotificationManager.class).cancel(id);
        }
        String item = intent.getStringExtra("item");
        if (item == null || item.isEmpty()) {
            return "";
        }
        try {
            String where = intent.getStringExtra("where");
            return new JSONObject().put("item", item).put("where", where == null ? "" : where)
                    .put("delist", intent.getBooleanExtra("delist", true)).toString();
        } catch (Exception e) {
            return "";
        }
    }

    @SuppressWarnings("deprecation")
    private static List<Uri> sharedUris(Intent intent) {
        List<Uri> uris = new ArrayList<>();
        if (Intent.ACTION_SEND_MULTIPLE.equals(intent.getAction())) {
            ArrayList<Uri> list = intent.getParcelableArrayListExtra(Intent.EXTRA_STREAM);
            if (list != null) {
                uris.addAll(list);
            }
        } else {
            Uri one = intent.getParcelableExtra(Intent.EXTRA_STREAM);
            if (one != null) {
                uris.add(one);
            }
        }
        ClipData clip = intent.getClipData();
        if (uris.isEmpty() && clip != null) {
            for (int i = 0; i < clip.getItemCount(); i++) {
                if (clip.getItemAt(i).getUri() != null) {
                    uris.add(clip.getItemAt(i).getUri());
                }
            }
        }
        return uris.size() > MAX_SHARED ? new ArrayList<>(uris.subList(0, MAX_SHARED)) : uris;
    }

    /** Egy megosztott fotó a gép felületének: /__jarvis_shared/<köteg>/<sorszám> (a work.js kéri le). */
    private WebResourceResponse sharedPhoto(String rest) {
        List<Uri> photos = sharedPhotos;
        int batch;
        int index;
        try {
            String[] parts = rest.split("/");
            batch = Integer.parseInt(parts[0]);
            index = Integer.parseInt(parts[1]);
        } catch (RuntimeException e) {
            return missing();
        }
        if (batch != sharedBatch || index < 0 || index >= photos.size()) {
            return missing();
        }
        Uri uri = photos.get(index);
        try {
            String mime = getContentResolver().getType(uri);
            String name = displayName(uri, index);
            InputStream data;
            if (("image/heic".equals(mime) || "image/heif".equals(mime)) && Build.VERSION.SDK_INT >= 28) {
                data = new ByteArrayInputStream(jpeg(uri));    // a gép a HEIC-et külön csomag nélkül nem ismeri
                mime = "image/jpeg";
                name = name.replaceFirst("(?i)\\.hei[cf]$", "") + ".jpg";
            } else {
                data = getContentResolver().openInputStream(uri);
            }
            Map<String, String> headers = new HashMap<>();
            headers.put("X-Jarvis-Name", Uri.encode(name));
            headers.put("Cache-Control", "no-store");
            return new WebResourceResponse(mime == null ? "image/jpeg" : mime, null, 200, "OK", headers, data);
        } catch (Exception e) {                                 // pl. közben lejárt az olvasási engedély
            return missing();
        }
    }

    private static WebResourceResponse missing() {
        return new WebResourceResponse("text/plain", "utf-8", 404, "Not Found", new HashMap<>(),
                new ByteArrayInputStream(new byte[0]));
    }

    private String displayName(Uri uri, int index) {
        try (Cursor cursor = getContentResolver().query(uri, new String[]{OpenableColumns.DISPLAY_NAME}, null, null,
                null)) {
            if (cursor != null && cursor.moveToFirst()) {
                String name = cursor.getString(0);
                if (name != null && !name.isEmpty()) {
                    return name;
                }
            }
        } catch (RuntimeException e) {
            // nem minden megosztó adja meg a nevet
        }
        return "foto-" + (index + 1) + ".jpg";
    }

    /** HEIC/HEIF kép JPEG-ben (legfeljebb kb. 3000 pixel; az elforgatást az ImageDecoder kezeli). */
    @TargetApi(28)
    private byte[] jpeg(Uri uri) throws IOException {
        Bitmap bitmap = ImageDecoder.decodeBitmap(ImageDecoder.createSource(getContentResolver(), uri),
                (decoder, info, source) -> {
                    int side = Math.max(info.getSize().getWidth(), info.getSize().getHeight());
                    int sample = 1;
                    while (side / sample > 3000) {
                        sample *= 2;
                    }
                    decoder.setTargetSampleSize(sample);
                });
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        bitmap.compress(Bitmap.CompressFormat.JPEG, 90, out);
        bitmap.recycle();
        return out.toByteArray();
    }

    /** A gép felülete, ha párosítva van; különben a beállító oldal. */
    private void open() {
        if (prefs.paired()) {
            web.loadUrl(prefs.server() + "/?t=" + Uri.encode(prefs.token()));
            NotifyService.start(this, prefs);
        } else {
            web.loadUrl(SETUP);
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        visible = true;
        web.onResume();
    }

    @Override
    protected void onPause() {
        visible = false;
        web.onPause();
        super.onPause();
    }

    /** Háttérben (SLEEP_AFTER_MS után) a felület élő csatornája bezárul, és az időzítői megállnak (sleep). */
    private boolean asleep;
    private final Runnable sleep = () -> {
        web.evaluateJavascript("window.jarvisSleep && window.jarvisSleep()", null);
        web.pauseTimers();
        asleep = true;
    };

    @Override
    protected void onStart() {
        super.onStart();
        web.removeCallbacks(sleep);
        web.resumeTimers();                            // (a megállítás az egész folyamatra szól: mindig visszaállítjuk)
        if (asleep) {                                  // előjött: újra él (a gép visszajátssza, ami közben történt)
            asleep = false;
            web.evaluateJavascript("window.jarvisWake && window.jarvisWake()", null);
        }
    }

    @Override
    protected void onStop() {
        // Ha egy ideig háttérben marad (nem csak egy fotóért vagy egy másolásért ugrottál át), a felület nem tartja
        // nyitva a kapcsolatot: a csatorna életjele felébresztené a telefon rádióját és a Tailscale-t. Az értesítéseket
        // közben is a háttérszolgáltatás hozza.
        web.removeCallbacks(sleep);
        web.postDelayed(sleep, SLEEP_AFTER_MS);
        super.onStop();
    }

    @Override
    protected void onDestroy() {
        web.removeCallbacks(sleep);                    // egy bezárt ablak ne állítsa meg a következő időzítőit
        super.onDestroy();
    }

    @Override
    public void onBackPressed() {
        if (web.canGoBack()) {
            web.goBack();
        } else {
            super.onBackPressed();
        }
    }

    /** A gép vagy a Kreatív menü letöltései (pl. egy történet .md-ben) a Letöltések mappába. */
    private void download(String url, String disposition, String mime) {
        try {
            String name = URLUtil.guessFileName(url, disposition, mime);
            DownloadManager.Request request = new DownloadManager.Request(Uri.parse(url));
            request.setTitle(name);
            request.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
            request.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, name);
            getSystemService(DownloadManager.class).enqueue(request);
            Toast.makeText(this, "Letöltés: " + name, Toast.LENGTH_SHORT).show();
        } catch (RuntimeException e) {
            Toast.makeText(this, "Nem sikerült letölteni.", Toast.LENGTH_SHORT).show();
        }
    }

    /** A saját oldalaink: a beállító oldal és a gép felülete. Minden más (pl. egy link) a böngészőben nyílik. */
    private boolean ours(Uri url) {
        if (url == null) {
            return false;
        }
        if ("file".equals(url.getScheme())) {
            return true;
        }
        String host = Uri.parse(prefs.server()).getHost();
        return host != null && host.equalsIgnoreCase(url.getHost());
    }

    /** Hogy az értesítések akkor is jöjjenek, ha a telefon energiatakarékos módban van (egyszer kérdezi). */
    private void askBattery(boolean background) {
        if (!background) {
            return;
        }
        PowerManager power = getSystemService(PowerManager.class);
        if (power == null || power.isIgnoringBatteryOptimizations(getPackageName())) {
            return;
        }
        try {
            startActivity(new Intent(android.provider.Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                    Uri.parse("package:" + getPackageName())));
        } catch (ActivityNotFoundException e) {
            // nem minden telefonon van ilyen beállítás
        }
    }

    static String explain(Exception error) {
        if (error instanceof UnknownHostException || error instanceof ConnectException
                || error instanceof SocketTimeoutException) {
            return "Nem érem el a gépet. Be van kapcsolva a Tailscale a telefonon, és fut a Jarvis a gépen?";
        }
        String message = error.getMessage();
        return message == null || message.isEmpty() ? "Nem sikerült." : message;
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == TAKE_PHOTO && filesCallback != null) {       // a kamerával készült fotó (Pro feltöltés)
            filesCallback.onReceiveValue(resultCode == RESULT_OK && cameraShot != null ? new Uri[]{cameraShot} : null);
            filesCallback = null;
            cameraShot = null;
            return;
        }
        if (requestCode != PICK_FILES || filesCallback == null) {
            return;
        }
        Uri[] result = null;
        if (resultCode == RESULT_OK && data != null) {
            ClipData clip = data.getClipData();
            if (clip != null) {
                result = new Uri[clip.getItemCount()];
                for (int i = 0; i < clip.getItemCount(); i++) {
                    result[i] = clip.getItemAt(i).getUri();
                }
            } else if (data.getData() != null) {
                result = new Uri[]{data.getData()};
            }
        }
        filesCallback.onReceiveValue(result);
        filesCallback = null;
    }

    private final class Client extends WebViewClient {
        @Override
        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
            Uri url = request.getUrl();
            if (ours(url)) {
                return false;
            }
            try {
                startActivity(new Intent(Intent.ACTION_VIEW, url));
            } catch (ActivityNotFoundException e) {
                // nincs mivel megnyitni
            }
            return true;
        }

        @Override
        public void onPageStarted(WebView view, String url, Bitmap favicon) {
            currentUrl = url == null ? "" : url;
        }

        /** A megosztott fotók a gép felületének (a WebView szálán fut; a kép a telefonról jön). */
        @Override
        public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
            Uri url = request.getUrl();
            String path = url.getPath();
            if (path == null || !path.startsWith(SHARED_PATH) || "file".equals(url.getScheme()) || !ours(url)) {
                return null;
            }
            return sharedPhoto(path.substring(SHARED_PATH.length()));
        }

        @Override
        public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
            if (request.isForMainFrame() && !"file".equals(request.getUrl().getScheme())) {
                view.loadUrl(SETUP + "#offline");
            }
        }

        /** A gép be van kapcsolva, de a Jarvis nem fut rajta: a Tailscale ilyenkor 502-vel felel. */
        @Override
        public void onReceivedHttpError(WebView view, WebResourceRequest request, WebResourceResponse response) {
            if (request.isForMainFrame() && response.getStatusCode() >= 500) {
                view.loadUrl(SETUP + "#offline");
            }
        }
    }

    private final class Chrome extends WebChromeClient {
        @Override
        public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback, FileChooserParams params) {
            if (filesCallback != null) {
                filesCallback.onReceiveValue(null);
            }
            filesCallback = callback;
            if (params.isCaptureEnabled()) {              // <input capture>: a telefon kamerája (Pro feltöltés)
                cameraShot = PhotoProvider.newShot(MainActivity.this);
                Intent camera = new Intent(MediaStore.ACTION_IMAGE_CAPTURE)
                        .putExtra(MediaStore.EXTRA_OUTPUT, cameraShot)
                        .addFlags(Intent.FLAG_GRANT_WRITE_URI_PERMISSION | Intent.FLAG_GRANT_READ_URI_PERMISSION);
                try {
                    startActivityForResult(camera, TAKE_PHOTO);
                    return true;
                } catch (ActivityNotFoundException e) {
                    cameraShot = null;                    // nincs kamera-alkalmazás: a sima fájlválasztó jön
                }
            }
            Intent intent = params.createIntent();
            intent.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, params.getMode() == FileChooserParams.MODE_OPEN_MULTIPLE);
            try {
                startActivityForResult(Intent.createChooser(intent, "Fotók"), PICK_FILES);
                return true;
            } catch (ActivityNotFoundException e) {
                filesCallback = null;
                return false;
            }
        }
    }

    /** A beállító oldal (assets/setup.html) és a gép belépő oldala ezen át szól az alkalmazásnak. */
    private final class Bridge {
        @JavascriptInterface
        public String state() {
            try {
                String server = pairServer.isEmpty() ? prefs.server() : pairServer;
                return new JSONObject().put("server", server).put("device", Build.MODEL)
                        .put("background", prefs.background()).put("code", pairCode).toString();
            } catch (Exception e) {
                return "{}";
            }
        }

        /** Amit egy parancsikon, a csempe, egy értesítés gombja vagy egy megosztás kért (egyszer adjuk oda). */
        @JavascriptInterface
        public String pending() {
            try {
                JSONObject result = new JSONObject().put("listing", wantListing)
                        .put("text", sharedText);
                if (photosNew) {
                    result.put("photos", sharedPhotos.size()).put("batch", sharedBatch);
                }
                if (!wantSold.isEmpty()) {
                    result.put("sold", new JSONObject(wantSold));
                }
                if (!wantDeal.isEmpty()) {
                    result.put("deal", wantDeal);
                }
                if (!wantItem.isEmpty()) {
                    result.put("item", new JSONObject(wantItem));
                }
                wantListing = false;
                sharedText = "";
                photosNew = false;
                wantSold = "";
                wantDeal = "";
                wantItem = "";
                return result.toString();
            } catch (Exception e) {
                return "{}";
            }
        }

        @JavascriptInterface
        public void pair(String server, String code, String device, boolean background) {
            if (!currentUrl.startsWith("file:")) {
                return;                                   // csak a beállító oldalról
            }
            String address = Api.normalize(server);
            new Thread(() -> {
                String error = null;
                try {
                    JSONObject result = Api.pair(address, code.trim(), device.trim());
                    prefs.save(address, result.getString("token"), background);
                    pairServer = "";
                    pairCode = "";
                } catch (Exception e) {
                    error = explain(e);
                }
                String message = error;
                runOnUiThread(() -> {
                    if (message == null) {
                        askBattery(background);
                        open();
                    } else {
                        web.evaluateJavascript("window.paired(false, " + JSONObject.quote(message) + ")", null);
                    }
                });
            }, "jarvis-pair").start();
        }

        /** A felület egy szövege a vágólapra (pl. a megérő hirdetés ablakából az eladónak szóló üzenet, vagy a
         *  Szakértői tipp kérdése). */
        @JavascriptInterface
        public void copy(String text) {
            runOnUiThread(() -> {
                ClipboardManager clipboard = getSystemService(ClipboardManager.class);
                if (clipboard != null) {
                    clipboard.setPrimaryClip(ClipData.newPlainText("Jarvis", text == null ? "" : text));
                    Toast.makeText(MainActivity.this, "Kimásoltam a vágólapra.", Toast.LENGTH_SHORT).show();
                }
            });
        }

        /** Szakértői tipp: a ChatGPT alkalmazás (ha telepítve van), különben a chatgpt.com a böngészőben. */
        @JavascriptInterface
        public void openChatGpt() {
            runOnUiThread(() -> {
                Intent launch = getPackageManager().getLaunchIntentForPackage(CHATGPT_PACKAGE);
                try {
                    startActivity(launch != null ? launch : new Intent(Intent.ACTION_VIEW, Uri.parse(CHATGPT_URL)));
                } catch (ActivityNotFoundException e) {
                    Toast.makeText(MainActivity.this, "Nem tudtam megnyitni a ChatGPT-t.", Toast.LENGTH_SHORT).show();
                }
            });
        }

        @JavascriptInterface
        public void retry() {
            runOnUiThread(MainActivity.this::open);
        }

        /** Újrapárosítás (pl. másik géppel): a kulcs elfelejtése. */
        @JavascriptInterface
        public void repair() {
            prefs.forget();
            stopService(new Intent(MainActivity.this, NotifyService.class));
            runOnUiThread(() -> web.loadUrl(SETUP));
        }

        /** A gép belépő oldala szól: a kulcsot már nem fogadja el (a gépen visszavontad). */
        @JavascriptInterface
        public void revoked() {
            prefs.forget();
            stopService(new Intent(MainActivity.this, NotifyService.class));
            runOnUiThread(() -> web.loadUrl(SETUP + "#revoked"));
        }
    }
}
