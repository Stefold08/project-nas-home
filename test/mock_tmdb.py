#!/usr/bin/env python3
"""Finto TMDB per i test: risponde come le API vere per pochi film."""
import io
import json
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image, ImageDraw, ImageFont

GOOD_KEY = "0123456789abcdef0123456789abcdef"
GOOD_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJ0ZXN0Ijp0cnVlfQ.firma"

MOVIES = {
    603: {
        "id": 603, "title": "Matrix", "original_title": "The Matrix", "release_date": "1999-03-30",
        "runtime": 136, "vote_average": 8.22, "tagline": "Benvenuto nel mondo reale.",
        "overview": "Thomas Anderson, programmatore di giorno e hacker di notte, scopre che il mondo in cui vive è una simulazione creata dalle macchine.",
        "genres": [{"id": 28, "name": "Azione"}, {"id": 878, "name": "Fantascienza"}],
        "poster_path": "/matrix_it.jpg",
        "posters": [("/matrix_it.jpg", "it", 5.5), ("/matrix_en.jpg", "en", 5.8), ("/matrix_clean.jpg", None, 5.2), ("/matrix_fr.jpg", "fr", 5.0)],
        "directors": ["Lana Wachowski", "Lilly Wachowski"],
        "cast": ["Keanu Reeves", "Laurence Fishburne", "Carrie-Anne Moss", "Hugo Weaving", "Joe Pantoliano", "Marcus Chong", "Gloria Foster"],
        "colors": ((20, 60, 30), (120, 220, 120)),
    },
    11216: {
        "id": 11216, "title": "Nuovo Cinema Paradiso", "original_title": "Nuovo Cinema Paradiso", "release_date": "1988-11-17",
        "runtime": 124, "vote_average": 8.4, "tagline": "",
        "overview": "Il regista Salvatore Di Vita torna in Sicilia per il funerale di Alfredo, il proiezionista che gli ha fatto amare il cinema.",
        "genres": [{"id": 18, "name": "Dramma"}, {"id": 10749, "name": "Romance"}],
        "poster_path": "/paradiso_it.jpg",
        "posters": [("/paradiso_it.jpg", "it", 5.6), ("/paradiso_en.jpg", "en", 5.3)],
        "directors": ["Giuseppe Tornatore"],
        "cast": ["Philippe Noiret", "Salvatore Cascio", "Marco Leonardi", "Jacques Perrin"],
        "colors": ((90, 30, 20), (240, 200, 140)),
    },
    999: {
        "id": 999, "title": "Film Raro", "original_title": "A Rare Film", "release_date": "1973-05-01",
        "runtime": 95, "vote_average": 6.1, "tagline": "",
        "overview": "", "overview_en": "An English-only description used when the Italian one is missing.",
        "genres": [{"id": 99, "name": "Documentario"}],
        "poster_path": None, "posters": [],
        "directors": ["Qualcuno"], "cast": [],
        "colors": ((40, 40, 40), (200, 200, 200)),
    },
}


def poster_bytes(name, size):
    stem = name.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    movie = next((m for m in MOVIES.values() if any(p[0].endswith(stem + ".jpg") for p in m["posters"])), None)
    bg, fg = movie["colors"] if movie else ((60, 60, 90), (230, 230, 230))
    width = {"w185": 185, "w500": 500, "w780": 780}.get(size, 300)
    height = int(width * 1.5)
    img = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(img)
    for i in range(0, height, max(4, height // 40)):
        shade = tuple(min(255, c + (i * 60 // height)) for c in bg)
        draw.rectangle([0, i, width, i + max(2, height // 80)], fill=shade)
    font = ImageFont.load_default(size=max(12, width // 9))
    label = (movie["title"] if movie else stem).upper()
    draw.multiline_text((width * 0.08, height * 0.62), "\n".join(label.split()), font=font, fill=fg, spacing=4)
    tag = stem.split("_")[-1]
    draw.text((width * 0.08, height * 0.08), tag, font=ImageFont.load_default(size=max(10, width // 16)), fill=fg)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, obj=None, body=None, ctype="application/json"):
        data = body if body is not None else json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        if url.path.startswith("/t/p/"):
            _, _, _, size, name = url.path.split("/", 4)
            return self.reply(200, body=poster_bytes(name, size), ctype="image/jpeg")
        auth = self.headers.get("Authorization", "")
        if q.get("api_key") != GOOD_KEY and auth != "Bearer " + GOOD_TOKEN:
            return self.reply(401, {"status_code": 7, "status_message": "Invalid API key"})
        if url.path == "/3/configuration":
            return self.reply(200, {"images": {"base_url": "x"}})
        if url.path == "/3/search/movie":
            query = q.get("query", "").lower()
            year = q.get("year")
            found = [m for m in MOVIES.values() if query and (query in m["title"].lower() or query in m["original_title"].lower())]
            if year:
                found = [m for m in found if m["release_date"].startswith(year)]
            results = [{"id": m["id"], "title": m["title"], "original_title": m["original_title"], "release_date": m["release_date"],
                        "poster_path": m["poster_path"], "overview": m["overview"]} for m in found]
            return self.reply(200, {"page": 1, "results": results})
        if url.path.startswith("/3/movie/"):
            try:
                movie = MOVIES[int(url.path.rsplit("/", 1)[-1])]
            except (KeyError, ValueError):
                return self.reply(404, {"status_code": 34})
            lang = q.get("language", "")
            data = {k: movie[k] for k in ("id", "title", "original_title", "release_date", "runtime", "vote_average", "tagline", "genres", "poster_path")}
            data["overview"] = movie["overview_en"] if lang.startswith("en") and movie.get("overview_en") else movie["overview"]
            if "credits" in q.get("append_to_response", ""):
                data["credits"] = {
                    "crew": [{"name": d, "job": "Director"} for d in movie["directors"]] + [{"name": "Tizio", "job": "Producer"}],
                    "cast": [{"name": c, "order": i} for i, c in enumerate(movie["cast"])],
                }
            if "images" in q.get("append_to_response", ""):
                data["images"] = {"posters": [{"file_path": p, "iso_639_1": l, "vote_average": v} for p, l, v in movie["posters"]]}
            return self.reply(200, data)
        return self.reply(404, {"status_code": 34})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9911
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
