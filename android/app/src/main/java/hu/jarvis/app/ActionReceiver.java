package hu.jarvis.app;

import android.app.Notification;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

import org.json.JSONObject;

/**
 * Az értesítések gombjai: a jóváhagyás (Engedélyezem / Mégse, POST /api/v1/confirm), és az állandó értesítés
 * Szünet / Folytatás gombja (POST /api/v1/pause, /api/v1/resume). A választ a gépnek küldi.
 */
public class ActionReceiver extends BroadcastReceiver {
    static final String CONFIRM = "hu.jarvis.app.CONFIRM";
    static final String PAUSE = "hu.jarvis.app.PAUSE";
    static final String RESUME = "hu.jarvis.app.RESUME";
    static final int RESULT_ID = 3;

    static PendingIntent confirm(Context context, int id, boolean approved) {
        Intent intent = new Intent(context, ActionReceiver.class).setAction(CONFIRM).putExtra("id", id)
                .putExtra("approved", approved);
        return PendingIntent.getBroadcast(context, id * 2 + (approved ? 1 : 0), intent,
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
    }

    static PendingIntent command(Context context, String action) {
        Intent intent = new Intent(context, ActionReceiver.class).setAction(action);
        return PendingIntent.getBroadcast(context, 0, intent,
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
    }

    @Override
    public void onReceive(Context context, Intent intent) {
        String action = intent.getAction();
        Context app = context.getApplicationContext();
        PendingResult result = goAsync();
        new Thread(() -> {
            try {
                if (PAUSE.equals(action) || RESUME.equals(action)) {
                    pauseOrResume(app, PAUSE.equals(action));
                } else {
                    answer(app, intent.getIntExtra("id", 0), intent.getBooleanExtra("approved", false));
                }
            } finally {
                result.finish();
            }
        }, "jarvis-action").start();
    }

    private static void answer(Context app, int id, boolean approved) {
        String text;
        try {
            Prefs prefs = new Prefs(app);
            Api.call(prefs.server(), prefs.token(), "POST", "/api/v1/confirm",
                    new JSONObject().put("id", id).put("approved", approved), 20000);
            text = approved ? "Engedélyezted." : "Nem engedélyezted.";
        } catch (Exception e) {
            text = "Nem sikerült elküldeni: " + MainActivity.explain(e);
        }
        NotificationManager manager = app.getSystemService(NotificationManager.class);
        manager.cancel(NotifyService.CONFIRM_BASE + id);
        manager.notify(NotifyService.CONFIRM_BASE + id, brief(app, text));
    }

    /** Szünet: a gépen minden modell leáll (a videókártya és a memória felszabadul); Folytatás: újra betölti. */
    private static void pauseOrResume(Context app, boolean pause) {
        try {
            Prefs prefs = new Prefs(app);
            Api.call(prefs.server(), prefs.token(), "POST", pause ? "/api/v1/pause" : "/api/v1/resume", null, 20000);
        } catch (Exception e) {     // sikernél nem szólunk: az állandó értesítés átvált (a gép eseményéből)
            app.getSystemService(NotificationManager.class).notify(RESULT_ID,
                    brief(app, "Nem sikerült: " + MainActivity.explain(e)));
        }
    }

    private static Notification brief(Context app, String text) {
        return new Notification.Builder(app, NotifyService.CHANNEL_NEWS)
                .setSmallIcon(R.drawable.ic_notify)
                .setContentTitle("Jarvis")
                .setContentText(text)
                .setTimeoutAfter(8000)
                .setAutoCancel(true)
                .build();
    }
}
