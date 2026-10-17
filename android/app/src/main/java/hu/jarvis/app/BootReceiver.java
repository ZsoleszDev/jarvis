package hu.jarvis.app;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** A telefon újraindulása (vagy az alkalmazás frissítése) után a háttérértesítések maguktól újraindulnak. */
public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        NotifyService.channels(context);
        NotifyService.start(context, new Prefs(context));
    }
}
