package hu.jarvis.app;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.graphics.drawable.Icon;
import android.media.AudioAttributes;
import android.media.RingtoneManager;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.service.notification.StatusBarNotification;

import java.util.Arrays;
import java.util.HashMap;
import java.util.Map;

import org.json.JSONArray;
import org.json.JSONObject;

/**
 * Háttérszolgáltatás: akkor is szól, ha az alkalmazás be van zárva. A gép eseményeit kérdezi le (hosszú
 * lekérdezéssel, a Tailscale-en át), és értesít, ha a Jarvis jóváhagyást kér (a gombokkal az értesítésből is
 * megadhatod), ha kész vagy elakadt egy feladat, vagy ha kérdez valamit. A Firebase nem kell hozzá: minden a te
 * gépedről jön. Egy elkeltnek látszó hirdetés értesítésén az „Eladva itt, vedd le a többiről” gomb az alkalmazást
 * a hirdetés Eladva ablakánál nyitja meg (innen nem vesz le semmit: a levételt ott hagyod jóvá).
 *
 * Az állandó értesítés mutatja, hogy éri-e el a gépet, és szünetel-e a Jarvis; a gombjaival hangutasítást adhatsz,
 * vagy szüneteltetheted (a gépen minden modell leáll, pl. játékhoz), és folytathatod.
 *
 * Egy megérő hirdetés (az Automata kereskedés találata) sürgős: akkor is szól, ha az alkalmazás nyitva van. A telefon
 * ALERT_MS ideig újra és újra rezeg (és ha be van kapcsolva a hang, csenget is), aztán QUIET_MS szünet, és ha addig nem
 * nyitottad meg vagy söpörted el, újra; legfeljebb ALERT_ROUNDS-szor. Rábökve az alkalmazás a találat ablakával nyílik
 * meg (MainActivity.ACTION_DEAL): ára, mennyiért mennek, mennyit kereshetsz vele, és az üzenet az eladónak.
 */
public class NotifyService extends Service {
    static final String CHANNEL_LINK = "kapcsolat";
    static final String CHANNEL_CONFIRM = "jovahagyas";
    static final String CHANNEL_NEWS = "ertesitesek";
    static final String CHANNEL_DEAL = "megero-hirdetes";
    static final String CHANNEL_DEAL_QUIET = "megero-hirdetes-csendes";
    static final long ALERT_MS = 2 * 60 * 1000;     // ennyi ideig rezeg / csenget egyfolytában
    static final long QUIET_MS = 10 * 60 * 1000;    // utána ennyi szünet
    static final int ALERT_ROUNDS = 6;              // legfeljebb ennyiszer (kb. egy óra), ha addig nem nyitod meg
    static final int ONGOING_ID = 1;
    static final int REVOKED_ID = 2;
    static final int CONFIRM_BASE = 1000;
    static final int NOTICE_BASE = 100000;

    private volatile boolean running;
    private volatile boolean connected = true;
    private volatile boolean paused;
    private String shown = "";
    private Thread worker;
    private final Handler timers = new Handler(Looper.getMainLooper());
    private final Map<Integer, Notification.Builder> deals = new HashMap<>();    // értesítés → a csendes változata
    private final Map<Integer, Integer> rounds = new HashMap<>();

    /** Elindítja (ha párosítva van, és kérted a háttérértesítéseket), különben leállítja. */
    static void start(Context context, Prefs prefs) {
        Intent intent = new Intent(context, NotifyService.class);
        if (!prefs.paired() || !prefs.background()) {
            context.stopService(intent);
            return;
        }
        try {
            context.startForegroundService(intent);
        } catch (RuntimeException e) {
            // pl. a rendszer épp nem engedi a háttérből; az alkalmazás következő megnyitásakor újra
        }
    }

    static void channels(Context context) {
        NotificationChannel link = new NotificationChannel(CHANNEL_LINK, "Kapcsolat a géppel",
                NotificationManager.IMPORTANCE_MIN);
        link.setDescription("Ez kell ahhoz, hogy a Jarvis akkor is szólhasson, ha az alkalmazás be van zárva. "
                + "Elrejtheted, attól még működik.");
        link.setShowBadge(false);
        NotificationChannel confirm = new NotificationChannel(CHANNEL_CONFIRM, "Jóváhagyás",
                NotificationManager.IMPORTANCE_HIGH);
        confirm.setDescription("Ha a Jarvis valamihez az engedélyedet kéri (pl. közzététel).");
        NotificationChannel news = new NotificationChannel(CHANNEL_NEWS, "Értesítések",
                NotificationManager.IMPORTANCE_DEFAULT);
        news.setDescription("Kész vagy elakadt feladat, kérdés egy tárgyról, új találat egy figyelőben.");
        NotificationChannel deal = new NotificationChannel(CHANNEL_DEAL, "Megérő hirdetés",
                NotificationManager.IMPORTANCE_HIGH);
        deal.setDescription("Új hirdetés, ami megéri: két percig rezeg (és ha be van kapcsolva a hang, csenget), "
                + "aztán tíz perc múlva újra, amíg meg nem nézed.");
        deal.enableVibration(true);
        deal.setVibrationPattern(new long[]{0, 900, 500, 900, 500});
        deal.setSound(RingtoneManager.getDefaultUri(RingtoneManager.TYPE_NOTIFICATION),
                new AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_NOTIFICATION_EVENT)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build());
        NotificationChannel dealQuiet = new NotificationChannel(CHANNEL_DEAL_QUIET, "Megérő hirdetés (szünetben)",
                NotificationManager.IMPORTANCE_LOW);
        dealQuiet.setDescription("A megérő hirdetés értesítése a két riasztás közti szünetben (hang és rezgés nélkül).");
        context.getSystemService(NotificationManager.class).createNotificationChannels(
                Arrays.asList(link, confirm, news, deal, dealQuiet));
    }

    static PendingIntent openApp(Context context) {
        Intent intent = new Intent(context, MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        return PendingIntent.getActivity(context, 0, intent,
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
    }

    static void show(Context context, String channel, int id, String title, String text) {
        show(context, channel, id, title, text, null);
    }

    static void show(Context context, String channel, int id, String title, String text, Notification.Action action) {
        show(context, channel, id, title, text, action, openApp(context));
    }

    static void show(Context context, String channel, int id, String title, String text, Notification.Action action,
                     PendingIntent content) {
        Notification.Builder builder = new Notification.Builder(context, channel)
                .setSmallIcon(R.drawable.ic_notify)
                .setContentTitle(title)
                .setContentText(text)
                .setStyle(new Notification.BigTextStyle().bigText(text))
                .setContentIntent(content)
                .setAutoCancel(true);
        if (action != null) {
            builder.addAction(action);
        }
        context.getSystemService(NotificationManager.class).notify(id, builder.build());
    }

    /**
     * Az elkeltnek látszó hirdetés értesítésének gombja (a notice "actions" listájából a "sold"): megnyitja az
     * alkalmazást, és a felület a hirdetés Eladva ablakát mutatja, előre kiválasztva (MainActivity.ACTION_SOLD).
     * Null, ha az értesítésnek nincs ilyen gombja.
     */
    static Notification.Action soldAction(Context context, JSONObject notice, int id) {
        JSONArray actions = notice.optJSONArray("actions");
        String item = notice.optString("item");
        for (int i = 0; actions != null && !item.isEmpty() && i < actions.length(); i++) {
            JSONObject action = actions.optJSONObject(i);
            if (action == null || !"sold".equals(action.optString("action"))) {
                continue;
            }
            Intent intent = new Intent(context, MainActivity.class).setAction(MainActivity.ACTION_SOLD)
                    .putExtra("item", item).putExtra("where", action.optString("where"))
                    .putExtra("delist", action.optBoolean("delist", true)).putExtra("notification", id)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            PendingIntent open = PendingIntent.getActivity(context, id, intent,
                    PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
            return new Notification.Action.Builder((Icon) null, action.optString("label", "Eladva itt"), open).build();
        }
        return null;
    }

    /**
     * Egy tárgyhoz tartozó értesítés (a notice "open" mezője: "item" = Jarvis kérdez róla, "review" = a Pro feltöltés
     * Nézd át ablaka): rábökve az alkalmazás ezzel nyílik meg (MainActivity.ACTION_ITEM). Különben csak megnyitja.
     */
    static PendingIntent itemIntent(Context context, JSONObject notice, int id) {
        String item = notice.optString("item");
        String open = notice.optString("open");
        if (item.isEmpty() || !("item".equals(open) || "review".equals(open))) {
            return openApp(context);
        }
        Intent intent = new Intent(context, MainActivity.class).setAction(MainActivity.ACTION_ITEM)
                .putExtra("item", item).putExtra("open", open).putExtra("notification", id)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        return PendingIntent.getActivity(context, id, intent,
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
    }

    @Override
    public void onCreate() {
        super.onCreate();
        channels(this);
    }

    /** Hangutasítás: megnyitja az ablakot, és a gép felülete azonnal felvesz (MainActivity.ACTION_VOICE). */
    static PendingIntent voice(Context context) {
        Intent intent = new Intent(context, MainActivity.class).setAction(MainActivity.ACTION_VOICE)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        return PendingIntent.getActivity(context, 1, intent,
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
    }

    private Notification ongoing() {
        String text = !connected ? "Nem érem el a gépet (újrapróbálom)"
                : paused ? "Szünetel (a háttérmunka is)" : "Kapcsolatban a géppel";
        return new Notification.Builder(this, CHANNEL_LINK)
                .setSmallIcon(R.drawable.ic_notify)
                .setContentTitle("Jarvis")
                .setContentText(text)
                .setContentIntent(openApp(this))
                .setOngoing(true)
                .addAction(new Notification.Action.Builder(Icon.createWithResource(this, R.drawable.ic_mic), "Hang",
                        voice(this)).build())
                .addAction(new Notification.Action.Builder(
                        Icon.createWithResource(this, paused ? R.drawable.ic_play : R.drawable.ic_pause),
                        paused ? "Folytatás" : "Szünet",
                        ActionReceiver.command(this, paused ? ActionReceiver.RESUME : ActionReceiver.PAUSE)).build())
                .build();
    }

    /** Az állandó értesítés frissítése, ha változott, amit mutat (a kapcsolat vagy a szünet). */
    private synchronized void refresh() {
        String now = connected + "/" + paused;
        if (now.equals(shown)) {
            return;
        }
        shown = now;
        getSystemService(NotificationManager.class).notify(ONGOING_ID, ongoing());
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        Notification ongoing = ongoing();
        synchronized (this) {
            shown = connected + "/" + paused;
        }
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(ONGOING_ID, ongoing, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        } else {
            startForeground(ONGOING_ID, ongoing);
        }
        if (worker == null || !worker.isAlive()) {
            running = true;
            worker = new Thread(this::loop, "jarvis-events");
            worker.start();
        }
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        timers.removeCallbacksAndMessages(null);
        running = false;
        if (worker != null) {
            worker.interrupt();
        }
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    private void loop() {
        Prefs prefs = new Prefs(this);
        long backoff = 2000;
        boolean synced = false;
        while (running) {
            String server = prefs.server();
            String token = prefs.token();
            if (server.isEmpty() || token.isEmpty()) {
                break;
            }
            try {
                if (!synced) {            // (újra)kapcsolódáskor: szünetel-e, és honnan folytatjuk
                    JSONObject status = Api.call(server, token, "GET", "/api/v1/status", null, 20000);
                    paused = status.optBoolean("paused");
                    if (prefs.lastEvent() < 0) {          // az első kapcsolódáskor a régi eseményekről nem szólunk
                        prefs.lastEvent(status.optLong("last_event", 0));
                    }
                    synced = true;
                    connected = true;
                    refresh();
                }
                long after = prefs.lastEvent();
                JSONObject result = Api.call(server, token, "GET", "/api/v1/events?after=" + after + "&wait=50",
                        null, 75000);
                JSONArray events = result.optJSONArray("events");
                for (int i = 0; events != null && i < events.length(); i++) {
                    handle(events.getJSONObject(i));
                }
                prefs.lastEvent(result.optLong("last", after));
                backoff = 2000;
            } catch (Api.Unauthorized e) {
                prefs.forget();
                show(this, CHANNEL_NEWS, REVOKED_ID, "Jarvis",
                        "A párosítás megszűnt. Nyisd meg az alkalmazást, és párosítsd újra.");
                break;
            } catch (Exception e) {
                // nincs kapcsolat (a gép ki van kapcsolva, vagy a Tailscale nem fut): később újra
                synced = false;
                connected = false;
                refresh();
                try {
                    Thread.sleep(backoff);
                } catch (InterruptedException stop) {
                    break;
                }
                backoff = Math.min(backoff * 2, 60000);
            }
        }
        if (running) {
            stopSelf();                                   // nincs párosítás; ha leállítottak, már nem kell
        }
    }

    /** A megérő hirdetés értesítése: rábökve a találat ablaka nyílik meg (MainActivity.ACTION_DEAL). */
    private Notification.Builder dealBuilder(String channel, int id, JSONObject notice) {
        Intent intent = new Intent(this, MainActivity.class).setAction(MainActivity.ACTION_DEAL)
                .putExtra("deal", notice.optString("deal")).putExtra("notification", id)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        PendingIntent open = PendingIntent.getActivity(this, id, intent,
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        String text = notice.optString("text");
        return new Notification.Builder(this, channel)
                .setSmallIcon(R.drawable.ic_notify)
                .setContentTitle(notice.optString("title", "Megéri"))
                .setContentText(text)
                .setStyle(new Notification.BigTextStyle().bigText(text))
                .setContentIntent(open)
                .setCategory(Notification.CATEGORY_RECOMMENDATION)
                .setAutoCancel(true);
    }

    /** Riasztás: ALERT_MS ideig ismétlődő rezgés (és hang), aztán szünet (quiet). Fő szálon fut. */
    private void dealAlert(int id, JSONObject notice) {
        NotificationManager manager = getSystemService(NotificationManager.class);
        deals.put(id, dealBuilder(CHANNEL_DEAL_QUIET, id, notice));
        rounds.put(id, 1);
        Notification loud = dealBuilder(CHANNEL_DEAL, id, notice).build();
        loud.flags |= Notification.FLAG_INSISTENT;      // amíg nem állítjuk le, újra és újra rezeg és csenget
        manager.notify(id, loud);
        timers.postDelayed(() -> quiet(id, notice), ALERT_MS);
    }

    /** A két riasztás közti szünet: az értesítés marad, de csendben; QUIET_MS múlva újra, ha még nem nézted meg. */
    private void quiet(int id, JSONObject notice) {
        NotificationManager manager = getSystemService(NotificationManager.class);
        Notification.Builder silent = deals.get(id);
        if (silent == null || !showing(id)) {
            forget(id);
            return;
        }
        manager.cancel(id);                             // a folyamatos rezgés így áll le
        manager.notify(id, silent.build());
        int round = rounds.containsKey(id) ? rounds.get(id) : ALERT_ROUNDS;
        if (round >= ALERT_ROUNDS) {
            forget(id);
            return;
        }
        timers.postDelayed(() -> again(id, notice), QUIET_MS);
    }

    private void again(int id, JSONObject notice) {
        if (!deals.containsKey(id) || !showing(id)) {
            forget(id);                                 // közben megnyitottad vagy elsöpörted
            return;
        }
        NotificationManager manager = getSystemService(NotificationManager.class);
        rounds.put(id, (rounds.containsKey(id) ? rounds.get(id) : 0) + 1);
        Notification loud = dealBuilder(CHANNEL_DEAL, id, notice).build();
        loud.flags |= Notification.FLAG_INSISTENT;
        manager.cancel(id);
        manager.notify(id, loud);
        timers.postDelayed(() -> quiet(id, notice), ALERT_MS);
    }

    private boolean showing(int id) {
        for (StatusBarNotification shown : getSystemService(NotificationManager.class).getActiveNotifications()) {
            if (shown.getId() == id) {
                return true;
            }
        }
        return false;
    }

    private void forget(int id) {
        deals.remove(id);
        rounds.remove(id);
    }

    private void handle(JSONObject event) {
        String type = event.optString("type");
        NotificationManager manager = getSystemService(NotificationManager.class);
        if ("state".equals(type)) {
            // A készenlét nem szünet: a modellek ilyenkor nincsenek betöltve, de a háttérmunka (pl. a kereskedés) megy,
            // és az első kérés felébreszti. Ezért az állandó értesítés sem változik tőle.
            paused = "paused".equals(event.optString("state"));
            refresh();
        } else if ("confirm".equals(type)) {
            if (MainActivity.visible) {
                return;                                   // a felületen úgyis feljön a kérdés
            }
            int id = event.optInt("id");
            String prompt = event.optString("prompt");
            Notification notification = new Notification.Builder(this, CHANNEL_CONFIRM)
                    .setSmallIcon(R.drawable.ic_notify)
                    .setContentTitle("Jóváhagyás kell")
                    .setContentText(prompt)
                    .setStyle(new Notification.BigTextStyle().bigText(prompt))
                    .setContentIntent(openApp(this))
                    .setAutoCancel(true)
                    .addAction(new Notification.Action.Builder((Icon) null, "Engedélyezem",
                            ActionReceiver.confirm(this, id, true)).build())
                    .addAction(new Notification.Action.Builder((Icon) null, "Mégse",
                            ActionReceiver.confirm(this, id, false)).build())
                    .build();
            manager.notify(CONFIRM_BASE + id, notification);
        } else if ("confirm_done".equals(type)) {
            manager.cancel(CONFIRM_BASE + event.optInt("id"));
        } else if ("notification".equals(type) && event.optJSONObject("notice") != null
                && !event.optJSONObject("notice").optString("deal").isEmpty()) {
            JSONObject notice = event.optJSONObject("notice");
            int id = NOTICE_BASE + (notice.optString("id").hashCode() & 0xffff);
            timers.post(() -> dealAlert(id, notice));          // megérő hirdetés: akkor is, ha az ablak nyitva van
        } else if ("notification".equals(type) && !MainActivity.visible) {
            JSONObject notice = event.optJSONObject("notice");
            if (notice != null && !notice.optBoolean("quiet")) {   // a nem fontos csak az alkalmazás listájában
                int id = NOTICE_BASE + (notice.optString("id").hashCode() & 0xffff);
                show(this, CHANNEL_NEWS, id, notice.optString("title", "Jarvis"), notice.optString("text"),
                        soldAction(this, notice, id), itemIntent(this, notice, id));
            }
        }
    }
}
