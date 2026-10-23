package hu.jarvis.app;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.database.MatrixCursor;
import android.net.Uri;
import android.os.ParcelFileDescriptor;
import android.provider.OpenableColumns;

import java.io.File;
import java.io.FileNotFoundException;
import java.util.Arrays;
import java.util.Comparator;

/**
 * A kamerával készülő fotó helye (a Pro feltöltés Kamera gombja): a kamera-alkalmazás ide írja a képet
 * (MediaStore.EXTRA_OUTPUT), a WebView innen olvassa be. Csak az alkalmazás gyorsítótárának „kamera” mappáját szolgálja
 * ki; más alkalmazás csak az intenttel kapott joggal éri el (FLAG_GRANT_*_URI_PERMISSION), a galériába nem kerül.
 */
public final class PhotoProvider extends ContentProvider {
    static final String AUTHORITY = "hu.jarvis.app.photos";
    private static final String DIR = "kamera";
    private static final int KEEP = 10;                    // ennél több régi fotó nem marad a gyorsítótárban

    /** Egy új fotó helye (a régebbieket közben eltakarítja). */
    static Uri newShot(Context context) {
        File dir = new File(context.getCacheDir(), DIR);
        dir.mkdirs();
        File[] old = dir.listFiles();
        if (old != null && old.length >= KEEP) {
            Arrays.sort(old, Comparator.comparingLong(File::lastModified));
            for (int i = 0; i <= old.length - KEEP; i++) {
                old[i].delete();
            }
        }
        String name = "foto-" + System.currentTimeMillis() + ".jpg";
        return new Uri.Builder().scheme("content").authority(AUTHORITY).appendPath(name).build();
    }

    private File file(Uri uri) throws FileNotFoundException {
        String name = uri.getLastPathSegment();
        if (name == null || !name.matches("foto-\\d+\\.jpg")) {
            throw new FileNotFoundException(String.valueOf(uri));
        }
        return new File(new File(getContext().getCacheDir(), DIR), name);
    }

    @Override
    public boolean onCreate() {
        return true;
    }

    @Override
    public ParcelFileDescriptor openFile(Uri uri, String mode) throws FileNotFoundException {
        return ParcelFileDescriptor.open(file(uri), ParcelFileDescriptor.parseMode(mode));
    }

    @Override
    public String getType(Uri uri) {
        return "image/jpeg";
    }

    @Override
    public Cursor query(Uri uri, String[] projection, String selection, String[] args, String sort) {
        File file;
        try {
            file = file(uri);
        } catch (FileNotFoundException e) {
            return null;
        }
        String[] columns = projection != null ? projection
                : new String[]{OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE};
        Object[] row = new Object[columns.length];
        for (int i = 0; i < columns.length; i++) {
            if (OpenableColumns.DISPLAY_NAME.equals(columns[i])) {
                row[i] = file.getName();
            } else if (OpenableColumns.SIZE.equals(columns[i])) {
                row[i] = file.length();
            }
        }
        MatrixCursor cursor = new MatrixCursor(columns);
        cursor.addRow(row);
        return cursor;
    }

    @Override
    public Uri insert(Uri uri, ContentValues values) {
        return null;
    }

    @Override
    public int delete(Uri uri, String selection, String[] args) {
        return 0;
    }

    @Override
    public int update(Uri uri, ContentValues values, String selection, String[] args) {
        return 0;
    }
}
