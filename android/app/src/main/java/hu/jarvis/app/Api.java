package hu.jarvis.app;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

import org.json.JSONException;
import org.json.JSONObject;

/** A gépen futó Jarvis telefonos kapcsolata (api.py), a Tailscale-en át: párosítás, események, jóváhagyás. */
final class Api {
    /** A gép nem fogadja el a kulcsot (visszavontad, vagy újra kell párosítani). */
    static final class Unauthorized extends IOException {
        private static final long serialVersionUID = 1L;

        Unauthorized() {
            super("A párosítás megszűnt.");
        }
    }

    private Api() {
    }

    /** "gep.tailnet.ts.net" → "https://gep.tailnet.ts.net" (a végéről a / lekerül). */
    static String normalize(String input) {
        String server = input == null ? "" : input.trim();
        while (server.endsWith("/")) {
            server = server.substring(0, server.length() - 1);
        }
        if (!server.isEmpty() && !server.startsWith("https://") && !server.startsWith("http://")) {
            server = "https://" + server;
        }
        return server;
    }

    static JSONObject call(String server, String token, String method, String path, JSONObject body,
                           int timeoutMs) throws IOException, JSONException {
        HttpURLConnection connection = (HttpURLConnection) new URL(server + path).openConnection();
        try {
            connection.setRequestMethod(method);
            connection.setConnectTimeout(15000);
            connection.setReadTimeout(timeoutMs);
            connection.setRequestProperty("Accept", "application/json");
            if (token != null && !token.isEmpty()) {
                connection.setRequestProperty("Authorization", "Bearer " + token);
            }
            if (body != null) {
                byte[] data = body.toString().getBytes(StandardCharsets.UTF_8);
                connection.setDoOutput(true);
                connection.setRequestProperty("Content-Type", "application/json; charset=utf-8");
                connection.setFixedLengthStreamingMode(data.length);
                try (OutputStream out = connection.getOutputStream()) {
                    out.write(data);
                }
            }
            int status = connection.getResponseCode();
            InputStream in = status >= 400 ? connection.getErrorStream() : connection.getInputStream();
            String text = in == null ? "" : read(in);
            if (status == 401) {
                throw new Unauthorized();
            }
            JSONObject result = text.isEmpty() ? new JSONObject() : new JSONObject(text);
            if (status >= 400) {
                throw new IOException(result.optString("error", "A gép hibát adott (" + status + ")."));
            }
            return result;
        } finally {
            connection.disconnect();
        }
    }

    static JSONObject pair(String server, String code, String device) throws IOException, JSONException {
        JSONObject body = new JSONObject().put("code", code).put("device", device);
        return call(server, null, "POST", "/api/v1/pair", body, 20000);
    }

    private static String read(InputStream in) throws IOException {
        try (InputStream stream = in) {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buffer = new byte[8192];
            int count;
            while ((count = stream.read(buffer)) > 0) {
                out.write(buffer, 0, count);
            }
            return out.toString("UTF-8");
        }
    }
}
