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
import android.os.Build;
import android.os.IBinder;

import java.util.Arrays;

import org.json.JSONArray;
import org.json.JSONObject;

/**
 * Háttérszolgáltatás: akkor is szól, ha az alkalmazás be van zárva. A gép eseményeit kérdezi le (hosszú
 * lekérdezéssel, a Tailscale-en át), és értesít, ha a Jarvis jóváhagyást kér (a gombokkal az értesítésből is
 * megadhatod), ha kész vagy elakadt egy feladat, vagy ha kérdez valamit. A Firebase nem kell hozzá: minden a te
 * gépedről jön.
 *
 * Az állandó értesítés mutatja, hogy éri-e el a gépet, és szünetel-e a Jarvis; a gombjaival hangutasítást adhatsz,
 * vagy szüneteltetheted (a gépen minden modell leáll, pl. játékhoz), és folytathatod.
 */
public class NotifyService extends Service {
    static final String CHANNEL_LINK = "kapcsolat";
    static final String CHANNEL_CONFIRM = "jovahagyas";
    static final String CHANNEL_NEWS = "ertesitesek";
    static final int ONGOING_ID = 1;
    static final int REVOKED_ID = 2;
    static final int CONFIRM_BASE = 1000;
    static final int NOTICE_BASE = 100000;

    private volatile boolean running;
    private volatile boolean connected = true;
    private volatile boolean paused;
    private String shown = "";
    private Thread worker;

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
        context.getSystemService(NotificationManager.class).createNotificationChannels(Arrays.asList(link, confirm, news));
    }

    static PendingIntent openApp(Context context) {
        Intent intent = new Intent(context, MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        return PendingIntent.getActivity(context, 0, intent,
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
    }

    static void show(Context context, String channel, int id, String title, String text) {
        Notification notification = new Notification.Builder(context, channel)
                .setSmallIcon(R.drawable.ic_notify)
                .setContentTitle(title)
                .setContentText(text)
                .setStyle(new Notification.BigTextStyle().bigText(text))
                .setContentIntent(openApp(context))
                .setAutoCancel(true)
                .build();
        context.getSystemService(NotificationManager.class).notify(id, notification);
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
                : paused ? "Szünetel: a modellek nem futnak" : "Kapcsolatban a géppel";
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

    private void handle(JSONObject event) {
        String type = event.optString("type");
        NotificationManager manager = getSystemService(NotificationManager.class);
        if ("state".equals(type)) {
            String state = event.optString("state");
            paused = "paused".equals(state) || "standby".equals(state);
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
        } else if ("notification".equals(type) && !MainActivity.visible) {
            JSONObject notice = event.optJSONObject("notice");
            if (notice != null) {
                show(this, CHANNEL_NEWS, NOTICE_BASE + (notice.optString("id").hashCode() & 0xffff),
                        notice.optString("title", "Jarvis"), notice.optString("text"));
            }
        }
    }
}
