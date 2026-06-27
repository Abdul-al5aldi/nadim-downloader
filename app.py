import os
import tempfile
import subprocess
from flask import Flask, request, send_file, jsonify
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TCON, APIC
import requests

app = Flask(__name__)

@app.route('/health')
def health():
    return jsonify({'status': 'ok'})

@app.route('/download', methods=['POST'])
def download():
    data = request.json
    url = data.get('url')
    title = data.get('title', '')
    artist = data.get('artist', '')
    album = data.get('album', '')
    genre = data.get('genre', '')
    image_url = data.get('image_url', '')

    if not url:
        return jsonify({'error': 'No URL provided'}), 400

    tmp_dir = tempfile.mkdtemp()
    output_path = os.path.join(tmp_dir, 'audio.mp3')

    try:
        # Download audio using yt-dlp
        subprocess.run([
            'yt-dlp',
            '-x',
            '--audio-format', 'mp3',
            '--audio-quality', '0',
            '-o', output_path,
            url
        ], check=True)

        # Tag the MP3
        audio = MP3(output_path, ID3=ID3)
        try:
            audio.add_tags()
        except Exception:
            pass

        if title:
            audio.tags.add(TIT2(encoding=3, text=title))
        if artist:
            audio.tags.add(TPE1(encoding=3, text=artist))
        if album:
            audio.tags.add(TALB(encoding=3, text=album))
        if genre:
            audio.tags.add(TCON(encoding=3, text=genre))

        # Add cover art if image URL provided
        if image_url:
            try:
                img_data = requests.get(image_url, timeout=10).content
                audio.tags.add(APIC(
                    encoding=3,
                    mime='image/jpeg',
                    type=3,
                    desc='Cover',
                    data=img_data
                ))
            except Exception:
                pass

        audio.save()

        # Return the file
        filename = f"{artist} - {title}.mp3" if artist and title else "audio.mp3"
        return send_file(
            output_path,
            as_attachment=True,
            download_name=filename,
            mimetype='audio/mpeg'
        )

    except subprocess.CalledProcessError as e:
        return jsonify({'error': f'Download failed: {str(e)}'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
