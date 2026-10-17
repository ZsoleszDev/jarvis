package hu.jarvis.app;

import android.app.PendingIntent;
import android.content.Intent;
import android.os.Build;
import android.service.quicksettings.Tile;
import android.service.quicksettings.TileService;

/**
 * Gyorsbeállítás csempe (a lehúzható értesítési sávban, a ceruza ikonnal adható hozzá): egy érintésre megnyílik
 * a Jarvis ablaka, és azonnal felveszi, amit mondasz (a felismerés a gépen történik).
 */
public class VoiceTile extends TileService {
    @Override
    public void onStartListening() {
        Tile tile = getQsTile();
        if (tile != null) {
            tile.setState(Tile.STATE_INACTIVE);
            tile.updateTile();
        }
    }

    @Override
    public void onClick() {
        if (isLocked()) {
            unlockAndRun(this::listen);
        } else {
            listen();
        }
    }

    @SuppressWarnings("deprecation")
    private void listen() {
        Intent intent = new Intent(this, MainActivity.class).setAction(MainActivity.ACTION_VOICE)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        if (Build.VERSION.SDK_INT >= 34) {
            startActivityAndCollapse(PendingIntent.getActivity(this, 0, intent,
                    PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT));
        } else {
            startActivityAndCollapse(intent);
        }
    }
}
