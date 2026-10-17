package hu.jarvis.app;

import android.Manifest;
import android.app.Activity;
import android.app.DownloadManager;
import android.content.ActivityNotFoundException;
import android.content.ClipData;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.os.PowerManager;
import android.webkit.JavascriptInterface;
import android.webkit.PermissionRequest;
import android.webkit.URLUtil;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

import java.net.ConnectException;
import java.net.SocketTimeoutException;
import java.net.UnknownHostException;

import org.json.JSONObject;

/**
 * A Jarvis ablaka a telefonon: a gépen futó Jarvis felülete (ugyanaz, mint a gépen), a Tailscale-en át. Minden a
 * gépen fut; a telefon csak mutatja, és innen adod a feladatokat. Párosítás nélkül a beállító oldal
 * (assets/setup.html) jön; ha a gép nem érhető el, ugyanaz, a hibával.
 */
public class MainActivity extends Activity {
    /** Látszik-e az ablak: ilyenkor a háttérszolgáltatás nem értesít (a felületen úgyis látod). */
    static volatile boolean visible;

    private static final String SETUP = "file:///android_asset/setup.html";
    private static final int PICK_FILES = 1;
    private static final int ASK_MIC = 2;
    private static final int ASK_NOTIFY = 3;

    private WebView web;
    private Prefs prefs;
    private volatile String currentUrl = "";
    private ValueCallback<Uri[]> filesCallback;
    private PermissionRequest micRequest;

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
        open();
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

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] results) {
        super.onRequestPermissionsResult(requestCode, permissions, results);
        if (requestCode == ASK_MIC && micRequest != null) {
            if (results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED) {
                micRequest.grant(new String[]{PermissionRequest.RESOURCE_AUDIO_CAPTURE});
            } else {
                micRequest.deny();
            }
            micRequest = null;
        }
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

        @Override
        public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
            if (request.isForMainFrame() && !"file".equals(request.getUrl().getScheme())) {
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

        @Override
        public void onPermissionRequest(PermissionRequest request) {
            runOnUiThread(() -> {
                boolean audioOnly = true;
                for (String resource : request.getResources()) {
                    if (!PermissionRequest.RESOURCE_AUDIO_CAPTURE.equals(resource)) {
                        audioOnly = false;
                    }
                }
                if (!audioOnly || !ours(request.getOrigin())) {
                    request.deny();
                    return;
                }
                if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
                    request.grant(new String[]{PermissionRequest.RESOURCE_AUDIO_CAPTURE});
                } else {
                    micRequest = request;
                    requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, ASK_MIC);
                }
            });
        }
    }

    /** A beállító oldal (assets/setup.html) és a gép belépő oldala ezen át szól az alkalmazásnak. */
    private final class Bridge {
        @JavascriptInterface
        public String state() {
            try {
                return new JSONObject().put("server", prefs.server()).put("device", Build.MODEL)
                        .put("background", prefs.background()).toString();
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
