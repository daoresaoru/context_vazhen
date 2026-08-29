import os
import re
import json
import sqlite3
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, jsonify, send_file
import io

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max upload
DB_PATH = 'tgaa.db'


# ─────────────────────────────────────────────
#  Database
# ─────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS files (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                raw_script  TEXT NOT NULL,
                uploaded_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS segments (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id      INTEGER NOT NULL REFERENCES files(id),
                seg_index    INTEGER NOT NULL,
                raw_segment  TEXT NOT NULL,
                translations TEXT NOT NULL DEFAULT '[]',
                status       TEXT NOT NULL DEFAULT 'untranslated'
            );
        ''')


# ─────────────────────────────────────────────
#  Parsing helpers (mirrors desktop app logic)
# ─────────────────────────────────────────────

_TAG_RE = re.compile(r'(<[^>]+>)')

CHARACTER_TAGS = {
    r'<E041 1 0>':    "Рюноскэ",
    r'<E041 3 2>':    "Сусато",
    r'<E041 4 4>':    "Айрис",
    r'<E041 2 1>':    "Шерлок",
    r'<E041 17 1>':   "Шерлок",
    r'<E041 78 56>':  "Вагахаи",
    r'<E041 96 59>':  "Виндибанк",
    r'<E041 47 31>':  "МакНат",
    r'<E041 10 6>':   "Джина",
    r'<E041 94 70>':  "???",
    r'<E041 95 57>':  "Том Летт",
    r'<E041 5 6>':    "Джина",
    r'<E041 7 9>':    "Грегсон",
    r'<E041 77 55>':  "Бобби",
    r'<E041 14 13>':  "Стронгхарт",
    r'<E041 75 24>':  "Пристав",
    r'<E041 15 15>':  "Судья",
    r'<E041 16 8>':   "ван Зикс",
    r'<E041 99 62>':  "Присяжный/-ая 1",
    r'<E041 52 63>':  "Присяжный/-ая 2",
    r'<E041 100 64>': "Присяжный/-ая 3",
    r'<E041 101 65>': "Присяжный/-ая 4",
    r'<E041 102 66>': "Присяжный/-ая 5",
    r'<E041 103 67>': "Присяжный/-ая 6",
    r'<E041 6 7>':    "ван Зикс",
    r'<E041 97 60>':  "Нэш (высокий)",
    r'<E041 94 58>':  "Грейдон",
}

STATUS_ORDER = ['untranslated', 'translated', 'edited', 'approved']
STATUS_LABELS = {
    'untranslated': 'Не переведено',
    'translated':   'Переведено',
    'edited':       'Отредактировано',
    'approved':     'Одобрено',
}
STATUS_COLORS = {
    'untranslated': '#888888',
    'translated':   '#558855',
    'edited':       '#555588',
    'approved':     '#885588',
}


def clean_text(segment):
    text = re.sub(r'<[^>]+>', ' ', segment)
    return re.sub(r'\s+', ' ', text).strip()


def extract_character(segment):
    for pattern, name in CHARACTER_TAGS.items():
        if re.search(pattern, segment):
            return name
    return None


def file_stats(file_id, db):
    rows = db.execute(
        'SELECT status FROM segments WHERE file_id = ?', (file_id,)
    ).fetchall()
    total = len(rows)
    counts = {s: 0 for s in STATUS_ORDER}
    for row in rows:
        counts[row['status']] = counts.get(row['status'], 0) + 1
    translated = total - counts['untranslated']
    pct = int(translated / total * 100) if total else 0
    return {
        'total':        total,
        'untranslated': counts['untranslated'],
        'translated':   counts['translated'],
        'edited':       counts['edited'],
        'approved':     counts['approved'],
        'pct':          pct,
    }


# ─────────────────────────────────────────────
#  Routes
# ─────────────────────────────────────────────

@app.route('/')
def index():
    db = get_db()
    files = db.execute('SELECT * FROM files ORDER BY uploaded_at DESC').fetchall()
    files_data = []
    total_all = translated_all = 0
    for f in files:
        stats = file_stats(f['id'], db)
        total_all += stats['total']
        translated_all += stats['total'] - stats['untranslated']
        files_data.append({'file': f, 'stats': stats})
    overall_pct = int(translated_all / total_all * 100) if total_all else 0
    return render_template('index.html',
                           files=files_data,
                           overall_pct=overall_pct,
                           total_all=total_all,
                           translated_all=translated_all,
                           status_colors=STATUS_COLORS)


@app.route('/upload', methods=['GET', 'POST'])
def upload():
    if request.method == 'POST':
        f = request.files.get('file')
        if not f:
            return "No file", 400

        content = f.read().decode('utf-8')
        name = f.filename

        # Detect format
        if name.endswith('.myformat') or name.endswith('.json'):
            data = json.loads(content)
            raw_script = data.get('raw_script', '')
            saved_segments = data.get('segments', [])
        else:
            raw_script = content
            saved_segments = []

        if not raw_script.strip():
            return "Empty script", 400

        db = get_db()

        # Check if file with same name exists — update instead of duplicate
        existing = db.execute('SELECT id FROM files WHERE name = ?', (name,)).fetchone()
        if existing:
            file_id = existing['id']
            db.execute('DELETE FROM segments WHERE file_id = ?', (file_id,))
            db.execute('UPDATE files SET raw_script = ?, uploaded_at = ? WHERE id = ?',
                       (raw_script, datetime.utcnow().isoformat(), file_id))
        else:
            cur = db.execute(
                'INSERT INTO files (name, raw_script, uploaded_at) VALUES (?, ?, ?)',
                (name, raw_script, datetime.utcnow().isoformat())
            )
            file_id = cur.lastrowid

        # Parse segments
        all_segs = raw_script.split('<PAGE>')
        row_idx = 0
        for seg_idx, seg in enumerate(all_segs):
            if not clean_text(seg):
                continue
            translations = '[]'
            status = 'untranslated'
            if row_idx < len(saved_segments):
                saved = saved_segments[row_idx]
                translations = json.dumps(saved.get('translations', []), ensure_ascii=False)
                status = saved.get('status', 'untranslated')
            db.execute(
                'INSERT INTO segments (file_id, seg_index, raw_segment, translations, status) '
                'VALUES (?, ?, ?, ?, ?)',
                (file_id, seg_idx, seg, translations, status)
            )
            row_idx += 1

        db.commit()
        return redirect(url_for('file_view', file_id=file_id))

    return render_template('upload.html')


@app.route('/file/<int:file_id>')
def file_view(file_id):
    db = get_db()
    f = db.execute('SELECT * FROM files WHERE id = ?', (file_id,)).fetchone()
    if not f:
        return "Not found", 404
    segments = db.execute(
        'SELECT * FROM segments WHERE file_id = ? ORDER BY seg_index',
        (file_id,)
    ).fetchall()
    stats = file_stats(file_id, db)

    seg_data = []
    for seg in segments:
        translations = json.loads(seg['translations'])
        character = extract_character(seg['raw_segment'])
        plain = clean_text(seg['raw_segment'])
        combined_translation = ' '.join(
            re.sub(r'<[^>]+>', '', t).strip()
            for t in translations if t.strip()
        )
        seg_data.append({
            'id':          seg['id'],
            'status':      seg['status'],
            'character':   character,
            'original':    plain,
            'translation': combined_translation,
        })

    return render_template('file.html',
                           file=f,
                           segments=seg_data,
                           stats=stats,
                           status_labels=STATUS_LABELS,
                           status_colors=STATUS_COLORS)


@app.route('/file/<int:file_id>/download')
def download(file_id):
    db = get_db()
    f = db.execute('SELECT * FROM files WHERE id = ?', (file_id,)).fetchone()
    if not f:
        return "Not found", 404
    segments = db.execute(
        'SELECT * FROM segments WHERE file_id = ? ORDER BY seg_index',
        (file_id,)
    ).fetchall()

    data = {
        'raw_script': f['raw_script'],
        'segments': [
            {
                'raw_segment':  seg['raw_segment'],
                'translations': json.loads(seg['translations']),
                'status':       seg['status'],
            }
            for seg in segments
        ]
    }
    buf = io.BytesIO(json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8'))
    buf.seek(0)
    download_name = f['name'].replace('.txt', '') + '.myformat'
    return send_file(buf, as_attachment=True,
                     download_name=download_name,
                     mimetype='application/json')


@app.route('/file/<int:file_id>/delete', methods=['POST'])
def delete_file(file_id):
    db = get_db()
    db.execute('DELETE FROM segments WHERE file_id = ?', (file_id,))
    db.execute('DELETE FROM files WHERE id = ?', (file_id,))
    db.commit()
    return redirect(url_for('index'))


@app.route('/api/segment/<int:seg_id>/status', methods=['POST'])
def update_status(seg_id):
    status = request.json.get('status')
    if status not in STATUS_ORDER:
        return jsonify({'error': 'invalid status'}), 400
    db = get_db()
    db.execute('UPDATE segments SET status = ? WHERE id = ?', (status, seg_id))
    db.commit()
    return jsonify({'ok': True})


if __name__ == '__main__':
    init_db()
    app.run(debug=True)
