from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import time
import os
import io
import tempfile
import traceback

from PIL import Image
from pillow_heif import register_heif_opener
import rawpy
import exifread

register_heif_opener()

app = Flask(__name__)
CORS(app)

ASTROMETRY_URL = "https://nova.astrometry.net/api"


@app.route("/")
def home():
    return jsonify({
        "status": "online",
        "service": "SkyScope Backend",
        "version": "2.0"
    })


def convert_to_jpeg(image_file, filename):
    """
    Converte HEIC/HEIF/DNG in JPEG.
    JPG e PNG vengono lasciati direttamente utilizzabili.
    """

    lower = filename.lower()

    # HEIC / HEIF
    if lower.endswith((".heic", ".heif")):

        image_file.seek(0)

        image = Image.open(image_file)

        output = io.BytesIO()

        image.convert("RGB").save(
            output,
            format="JPEG",
            quality=95
        )

        output.seek(0)

        return output, "converted.jpg", "image/jpeg"


    # DNG / RAW
    if lower.endswith((".dng", ".nef", ".cr2", ".cr3", ".arw", ".orf", ".rw2")):

        image_file.seek(0)

        raw_bytes = image_file.read()

        with tempfile.NamedTemporaryFile(
            suffix=".raw",
            delete=True
        ) as temp:

            temp.write(raw_bytes)
            temp.flush()

            with rawpy.imread(temp.name) as raw:

                rgb = raw.postprocess(
                    use_camera_wb=True,
                    no_auto_bright=True,
                    output_bps=8
                )

        image = Image.fromarray(rgb)

        output = io.BytesIO()

        image.save(
            output,
            format="JPEG",
            quality=95
        )

        output.seek(0)

        return output, "converted.jpg", "image/jpeg"


    # JPG / PNG
    image_file.seek(0)

    return image_file, filename, None


def read_exif(file_storage):
    """
    Legge data/ora e GPS dagli EXIF quando disponibili.
    """

    metadata = {
        "datetime": None,
        "latitude": None,
        "longitude": None
    }

    try:

        file_storage.seek(0)

        tags = exifread.process_file(
            file_storage,
            details=False
        )

        # Data e ora
        datetime_tag = (
            tags.get("EXIF DateTimeOriginal")
            or tags.get("EXIF DateTimeDigitized")
            or tags.get("Image DateTime")
        )

        if datetime_tag:
            metadata["datetime"] = str(datetime_tag)


        # GPS
        lat = tags.get("GPS GPSLatitude")
        lat_ref = tags.get("GPS GPSLatitudeRef")

        lon = tags.get("GPS GPSLongitude")
        lon_ref = tags.get("GPS GPSLongitudeRef")


        def convert_gps(value, reference):

            if not value:
                return None

            degrees = float(value.values[0].num) / float(value.values[0].den)

            minutes = float(value.values[1].num) / float(value.values[1].den)

            seconds = float(value.values[2].num) / float(value.values[2].den)

            decimal = degrees + minutes / 60 + seconds / 3600

            if str(reference) in ("S", "W"):
                decimal *= -1

            return decimal


        metadata["latitude"] = convert_gps(
            lat,
            lat_ref
        )

        metadata["longitude"] = convert_gps(
            lon,
            lon_ref
        )

    except Exception:
        pass

    return metadata


@app.route("/solve", methods=["POST"])
def solve():

    try:

        if "image" not in request.files:

            return jsonify({
                "error": "Nessuna immagine ricevuta"
            }), 400


        api_key = os.environ.get(
            "ASTROMETRY_API_KEY"
        )

        if not api_key:

            return jsonify({
                "error": "API key mancante su Render"
            }), 500


        image = request.files["image"]

        filename = image.filename or "image.jpg"


        # Legge gli EXIF prima della conversione
        exif_data = read_exif(image)


        # Converte se necessario
        upload_file, upload_filename, detected_mimetype = convert_to_jpeg(
            image.stream,
            filename
        )


        mimetype = (
            detected_mimetype
            or image.mimetype
            or "application/octet-stream"
        )


        session = requests.Session()


        # LOGIN
        login = session.post(
            ASTROMETRY_URL + "/login",
            data={
                "request-json": (
                    '{"apikey":"' +
                    api_key +
                    '"}'
                )
            },
            timeout=30
        )


        try:
            login_data = login.json()
        except Exception:

            return jsonify({
                "error": "Risposta non valida da Astrometry.net",
                "details": login.text[:500]
            }), 502


        if login_data.get("status") != "success":

            return jsonify({
                "error": "Login ad Astrometry.net fallito",
                "details": login_data
            }), 401


        session_key = login_data.get("session")


        if not session_key:

            return jsonify({
                "error": "Astrometry.net non ha restituito una sessione"
            }), 502


        # UPLOAD
        upload = session.post(

            ASTROMETRY_URL + "/upload",

            data={
                "request-json": (
                    '{"session":"' +
                    session_key +
                    '","publicly_visible":"n"}'
                )
            },

            files={
                "file": (
                    upload_filename,
                    upload_file,
                    mimetype
                )
            },

            timeout=60
        )


        try:
            result = upload.json()

        except Exception:

            return jsonify({
                "error": "Risposta non valida durante l'upload",
                "details": upload.text[:500]
            }), 502


        if result.get("status") != "success":

            return jsonify({
                "error": "Upload fallito",
                "details": result
            }), 500


        submission_id = result.get("subid")


        if not submission_id:

            return jsonify({
                "error": "Astrometry.net non ha restituito il submission ID"
            }), 502


        # ATTENDE IL JOB
        job_id = None


        for _ in range(60):

            time.sleep(2)

            response = session.get(
                f"{ASTROMETRY_URL}/submissions/{submission_id}",
                timeout=30
            )

            submission = response.json()

            jobs = submission.get("jobs") or []


            if jobs and jobs[0] is not None:

                job_id = jobs[0]

                break


        if job_id is None:

            return jsonify({
                "error": (
                    "Astrometry.net non ha assegnato "
                    "un job entro il tempo previsto"
                )
            }), 504


        # ATTENDE IL PLATE SOLVING
        solved = False


        for _ in range(90):

            time.sleep(2)

            response = session.get(
                f"{ASTROMETRY_URL}/jobs/{job_id}",
                timeout=30
            )

            job_data = response.json()

            status = job_data.get("status")


            if status == "success":

                solved = True

                break


            if status == "failure":

                return jsonify({
                    "error": (
                        "Astrometry.net non è riuscito "
                        "a risolvere questa foto"
                    )
                }), 422


        if not solved:

            return jsonify({
                "error": (
                    "Analisi troppo lenta. "
                    "Il server astronomico non ha terminato in tempo."
                )
            }), 504


        # INFORMAZIONI ASTRONOMICHE
        info_response = session.get(
            f"{ASTROMETRY_URL}/jobs/{job_id}/info",
            timeout=30
        )


        info = info_response.json()

        calibration = info.get(
            "calibration",
            {}
        )


        return jsonify({

            "success": True,

            "job_id": job_id,

            "ra": calibration.get("ra"),

            "dec": calibration.get("dec"),

            "pixscale": calibration.get("pixscale"),

            "orientation": calibration.get("orientation"),

            "exif": exif_data,

            "file_type": filename.split(".")[-1].lower()

        })


    except Exception as error:

        print("SKYSCOPE ERROR:")

        traceback.print_exc()


        return jsonify({

            "error": "Errore interno del server",

            "details": str(error)

        }), 500


if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=10000
    )
