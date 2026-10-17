package hu.jarvis.app;

import android.content.Context;
import android.content.SharedPreferences;

/** A párosítás adatai: a gép címe és az eszköz kulcsa (csak ezen a telefonon, az alkalmazás saját tárában). */
final class Prefs {
    private final SharedPreferences prefs;

    Prefs(Context context) {
        prefs = context.getSharedPreferences("jarvis", Context.MODE_PRIVATE);
    }

    String server() {
        return prefs.getString("server", "");
    }

    String token() {
        return prefs.getString("token", "");
    }

    boolean paired() {
        return !server().isEmpty() && !token().isEmpty();
    }

    /** Értesítések akkor is, ha az alkalmazás be van zárva (háttérszolgáltatás). */
    boolean background() {
        return prefs.getBoolean("background", true);
    }

    /** Az utolsó esemény sorszáma, amiről már tudunk (-1: még semmi; ilyenkor a régiekről nem szólunk). */
    long lastEvent() {
        return prefs.getLong("last_event", -1);
    }

    void lastEvent(long seq) {
        prefs.edit().putLong("last_event", seq).apply();
    }

    void save(String server, String token, boolean background) {
        prefs.edit().putString("server", server).putString("token", token).putBoolean("background", background)
                .putLong("last_event", -1).apply();
    }

    /** A gép már nem fogadja el a kulcsot (visszavontad), vagy újrapárosítasz: a cím megmarad. */
    void forget() {
        prefs.edit().remove("token").remove("last_event").apply();
    }
}
