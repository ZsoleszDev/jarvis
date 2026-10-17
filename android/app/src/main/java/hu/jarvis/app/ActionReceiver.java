package hu.jarvis.app;

import android.app.Notification;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

import org.json.JSONObject;

/** A jóváhagyás gombjai az értesítésben: a választ a gépnek küldi (POST /api/v1/confirm). */
public class ActionReceiver extends BroadcastReceiver {
    static PendingIntent intent(Context context, int id, boolean approved) {
        Intent intent = new Intent(context, ActionReceiver.class).putExtra("id", id).putExtra("approved", approved);
        return PendingIntent.getBroadcast(context, id * 2 + (approved ? 1 : 0), intent,
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
    }

    @Override
    public void onReceive(Context context, Intent intent) {
        int id = intent.getIntExtra("id", 0);
        boolean approved = intent.getBooleanExtra("approved", false);
        Context app = context.getApplicationContext();
        PendingResult result = goAsync();
        new Thread(() -> {
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
            Notification done = new Notification.Builder(app, NotifyService.CHANNEL_NEWS)
                    .setSmallIcon(R.drawable.ic_notify)
                    .setContentTitle("Jarvis")
                    .setContentText(text)
                    .setTimeoutAfter(8000)
                    .setAutoCancel(true)
                    .build();
            manager.notify(NotifyService.CONFIRM_BASE + id, done);
            result.finish();
        }, "jarvis-confirm").start();
    }
}
