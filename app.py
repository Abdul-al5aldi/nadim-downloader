import os
import io
import stat
import posixpath
import tempfile
import functools
from flask import Flask, request, jsonify, send_file, session, send_from_directory
import paramiko
from mutagen.mp3 import MP3
from mutagen.id3 import (
    ID3, TIT2, TPE1, TPE2, TALB, TCON, TDRC, TRCK, COMM, TCOM, TLAN, USLT, APIC
)

app = Flask(__name__, static_folder='static')
app.secret_key = os.environ.get('APP_SECRET', 'change-me-in-render-env')

# ---- Config from environment variables (set these in Render dashboard) ----
SFTP_HOST = os.environ.get('SFTP_HOST', 'sftp.pikapods.com')
SFTP_PORT = int(os.environ.get('SFTP_PORT', '2222'))
SFTP_USER = os.environ.get('SFTP_USER', '')
SFTP_PASS = os.environ.get('SFTP_PASS', '')
MUSIC_ROOT = os.environ.get('MUSIC_ROOT', '/music')
ACCESS_PASSWORD = os.environ.get('ACCESS_PASSWORD', '')

AUDIO_EXTS = {'.mp3'}
LYRIC_EXTS = {'.lrc', '.txt'}


# ---------------- SFTP helpers ----------------

def sftp_connect():
    transport = paramiko.Transport((SFTP_HOST, SFTP_PORT))
    transport.connect(username=SFTP_USER, password=SFTP_PASS)
    sftp = paramiko.SFTPClient.from_transport(transport)
    return sftp, transport


def safe_path(rel_path):
    """Prevent path traversal. All paths are relative to MUSIC_ROOT."""
    rel_path = (rel_path or '').strip('/')
    full = posixpath.normpath(posixpath.join(MUSIC_ROOT, rel_path))
    if not full.startswith(MUSIC_ROOT):
        raise ValueError('Invalid path')
    return full


# ---------------- Auth ----------------

def login_required(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get('authed'):
            return jsonify({'error': 'unauthorized'}), 401
        return f(*args, **kwargs)
    return wrapper


@app.route('/api/login', methods=['POST'])
def login():
    data = request.json or {}
    if ACCESS_PASSWORD and data.get('password') == ACCESS_PASSWORD:
        session['authed'] = True
        session.permanent = True
        return jsonify({'ok': True})
    return jsonify({'error': 'wrong password'}), 401


@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'ok': True})


@app.route('/api/check-auth')
def check_auth():
    return jsonify({'authed': bool(session.get('authed'))})


# ---------------- File browsing ----------------

@app.route('/api/list')
@login_required
def list_files():
    rel = request.args.get('path', '')
    full = safe_path(rel)
    sftp, transport = sftp_connect()
    try:
        entries = []
        for attr in sftp.listdir_attr(full):
            is_dir = stat.S_ISDIR(attr.st_mode)
            name = attr.filename
            ext = posixpath.splitext(name)[1].lower()
            entries.append({
                'name': name,
                'is_dir': is_dir,
                'size': attr.st_size,
                'is_audio': (not is_dir) and ext in AUDIO_EXTS,
                'is_lyric': (not is_dir) and ext in LYRIC_EXTS,
            })
        entries.sort(key=lambda e: (not e['is_dir'], e['name'].lower()))
        return jsonify({'path': rel, 'entries': entries})
    finally:
        sftp.close()
        transport.close()


# ---------------- Tag reading / writing ----------------

def download_to_temp(sftp, remote_path):
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.mp3')
    tmp.close()
    sftp.get(remote_path, tmp.name)
    return tmp.name


def get_text(tags, frame):
    f = tags.get(frame)
    return str(f.text[0]) if f and f.text else ''


@app.route('/api/quick-tags')
@login_required
def quick_tags():
    """Lightweight tag read for the file-list preview (title + artist only)."""
    rel = request.args.get('path', '')
    full = safe_path(rel)
    sftp, transport = sftp_connect()
    try:
        local = download_to_temp(sftp, full)
    finally:
        sftp.close()
        transport.close()

    try:
        audio = MP3(local, ID3=ID3)
        tags = audio.tags or ID3()
        return jsonify({
            'title': get_text(tags, 'TIT2'),
            'artist': get_text(tags, 'TPE1'),
        })
    except Exception:
        return jsonify({'title': '', 'artist': ''})
    finally:
        os.unlink(local)


@app.route('/api/tags')
@login_required
def read_tags():
    rel = request.args.get('path', '')
    full = safe_path(rel)
    sftp, transport = sftp_connect()
    try:
        local = download_to_temp(sftp, full)
    finally:
        sftp.close()
        transport.close()

    try:
        audio = MP3(local, ID3=ID3)
        tags = audio.tags or ID3()

        lyrics = ''
        for k in tags.keys():
            if k.startswith('USLT'):
                lyrics = str(tags[k].text)
                break

        lang = ''
        tlan = tags.get('TLAN')
        if tlan and tlan.text:
            lang = str(tlan.text[0])

        comment = ''
        for k in tags.keys():
            if k.startswith('COMM'):
                comment = str(tags[k].text[0]) if tags[k].text else ''
                break

        has_cover = any(k.startswith('APIC') for k in tags.keys())

        return jsonify({
            'title': get_text(tags, 'TIT2'),
            'artist': get_text(tags, 'TPE1'),
            'album_artist': get_text(tags, 'TPE2'),
            'album': get_text(tags, 'TALB'),
            'genre': get_text(tags, 'TCON'),
            'year': get_text(tags, 'TDRC'),
            'track': get_text(tags, 'TRCK'),
            'composer': get_text(tags, 'TCOM'),
            'language': lang,
            'comment': comment,
            'lyrics': lyrics,
            'has_cover': has_cover,
            'duration': round(audio.info.length) if audio.info else 0,
        })
    finally:
        os.unlink(local)


@app.route('/api/tags', methods=['POST'])
@login_required
def write_tags():
    data = request.json or {}
    rel = data.get('path', '')
    full = safe_path(rel)

    sftp, transport = sftp_connect()
    try:
        local = download_to_temp(sftp, full)

        audio = MP3(local, ID3=ID3)
        if audio.tags is None:
            audio.add_tags()
        tags = audio.tags

        def set_frame(key, frame_cls, value):
            for k in list(tags.keys()):
                if k.startswith(key):
                    del tags[k]
            if value:
                tags.add(frame_cls(encoding=3, text=value))

        set_frame('TIT2', TIT2, data.get('title', ''))
        set_frame('TPE1', TPE1, data.get('artist', ''))
        set_frame('TPE2', TPE2, data.get('album_artist', ''))
        set_frame('TALB', TALB, data.get('album', ''))
        set_frame('TCON', TCON, data.get('genre', ''))
        set_frame('TDRC', TDRC, data.get('year', ''))
        set_frame('TRCK', TRCK, data.get('track', ''))
        set_frame('TCOM', TCOM, data.get('composer', ''))
        set_frame('TLAN', TLAN, data.get('language', ''))

        # Comment
        for k in list(tags.keys()):
            if k.startswith('COMM'):
                del tags[k]
        if data.get('comment'):
            tags.add(COMM(encoding=3, lang='eng', desc='', text=data['comment']))

        # Lyrics
        for k in list(tags.keys()):
            if k.startswith('USLT'):
                del tags[k]
        if data.get('lyrics'):
            tags.add(USLT(encoding=3, lang=data.get('language') or 'eng',
                          desc='', text=data['lyrics']))

        # Cover art removal
        if data.get('remove_cover'):
            for k in list(tags.keys()):
                if k.startswith('APIC'):
                    del tags[k]

        audio.save()
        sftp.put(local, full)
        os.unlink(local)
        return jsonify({'ok': True})
    finally:
        sftp.close()
        transport.close()


# ---------------- Cover art ----------------

@app.route('/api/cover')
@login_required
def get_cover():
    rel = request.args.get('path', '')
    full = safe_path(rel)
    sftp, transport = sftp_connect()
    try:
        local = download_to_temp(sftp, full)
    finally:
        sftp.close()
        transport.close()

    try:
        audio = MP3(local, ID3=ID3)
        tags = audio.tags or ID3()
        for k in tags.keys():
            if k.startswith('APIC'):
                apic = tags[k]
                return send_file(io.BytesIO(apic.data),
                                 mimetype=apic.mime or 'image/jpeg')
        return jsonify({'error': 'no cover'}), 404
    finally:
        os.unlink(local)


@app.route('/api/cover', methods=['POST'])
@login_required
def set_cover():
    rel = request.form.get('path', '')
    full = safe_path(rel)
    img = request.files.get('image')
    if not img:
        return jsonify({'error': 'no image'}), 400
    img_data = img.read()
    mime = img.mimetype or 'image/jpeg'

    sftp, transport = sftp_connect()
    try:
        local = download_to_temp(sftp, full)
        audio = MP3(local, ID3=ID3)
        if audio.tags is None:
            audio.add_tags()
        tags = audio.tags
        for k in list(tags.keys()):
            if k.startswith('APIC'):
                del tags[k]
        tags.add(APIC(encoding=3, mime=mime, type=3, desc='Cover', data=img_data))
        audio.save()
        sftp.put(local, full)
        os.unlink(local)
        return jsonify({'ok': True})
    finally:
        sftp.close()
        transport.close()


# ---------------- File operations ----------------

@app.route('/api/upload', methods=['POST'])
@login_required
def upload_file():
    rel_dir = request.form.get('path', '')
    full_dir = safe_path(rel_dir)
    f = request.files.get('file')
    if not f:
        return jsonify({'error': 'no file'}), 400

    sftp, transport = sftp_connect()
    try:
        remote = posixpath.join(full_dir, f.filename)
        sftp.putfo(f.stream, remote)
        return jsonify({'ok': True})
    finally:
        sftp.close()
        transport.close()


@app.route('/api/mkdir', methods=['POST'])
@login_required
def make_dir():
    data = request.json or {}
    full = safe_path(data.get('path', ''))
    sftp, transport = sftp_connect()
    try:
        sftp.mkdir(full)
        return jsonify({'ok': True})
    finally:
        sftp.close()
        transport.close()


@app.route('/api/rename', methods=['POST'])
@login_required
def rename():
    data = request.json or {}
    src = safe_path(data.get('src', ''))
    dst = safe_path(data.get('dst', ''))
    sftp, transport = sftp_connect()
    try:
        sftp.rename(src, dst)
        return jsonify({'ok': True})
    finally:
        sftp.close()
        transport.close()


@app.route('/api/delete', methods=['POST'])
@login_required
def delete():
    data = request.json or {}
    full = safe_path(data.get('path', ''))
    sftp, transport = sftp_connect()
    try:
        st = sftp.stat(full)
        if stat.S_ISDIR(st.st_mode):
            sftp.rmdir(full)  # only works if empty
        else:
            sftp.remove(full)
        return jsonify({'ok': True})
    finally:
        sftp.close()
        transport.close()


# ---------------- Audio streaming (for preview player) ----------------

@app.route('/api/stream')
@login_required
def stream():
    rel = request.args.get('path', '')
    full = safe_path(rel)
    sftp, transport = sftp_connect()
    try:
        buf = io.BytesIO()
        sftp.getfo(full, buf)
        buf.seek(0)
        return send_file(buf, mimetype='audio/mpeg')
    finally:
        sftp.close()
        transport.close()


# ---------------- Frontend ----------------

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
