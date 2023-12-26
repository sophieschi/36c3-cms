import json
import logging
import os
import random
import shutil
import socket
import tempfile
import time
from datetime import datetime
from logging import basicConfig, getLogger
from secrets import token_hex

import iso8601
import paho.mqtt.client as mqtt
import requests
from flask import (
    Flask,
    abort,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_github import GitHub
from werkzeug.middleware.proxy_fix import ProxyFix

from conf import CONFIG
from helper import (
    error,
    get_all_live_assets,
    get_random,
    get_user_assets,
    login_disabled_for_user,
    mk_sig,
)
from ib_hosted import get_scoped_api_key, ib, update_asset_userdata
from redis_session import RedisSessionStore

basicConfig(
    format="[%(levelname)s %(name)s] %(message)s",
    level=logging.INFO,
)

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app)

for copy_key in (
    "GITHUB_CLIENT_ID",
    "GITHUB_CLIENT_SECRET",
    "MAX_UPLOADS",
    "TIME_MAX",
    "TIME_MIN",
):
    app.config[copy_key] = CONFIG[copy_key]

socket.setdefaulttimeout(3)  # for mqtt


github = GitHub(app)
app.session_interface = RedisSessionStore()


def cached_asset_name(asset):
    asset_id = asset["id"]
    filename = "asset-{}.{}".format(
        asset_id,
        "jpg" if asset["filetype"] == "image" else "mp4",
    )
    cache_name = f"static/{filename}"

    if not os.path.exists(cache_name):
        app.logger.info(f"fetching {asset_id} to {cache_name}")
        dl = ib.get(f"asset/{asset_id}/download")
        r = requests.get(dl["download_url"], stream=True, timeout=5)
        r.raise_for_status()
        with tempfile.NamedTemporaryFile(delete=False) as f:
            shutil.copyfileobj(r.raw, f)
            shutil.move(f.name, cache_name)
            os.chmod(cache_name, 0o664)
        del r

    return filename


@app.before_request
def before_request():
    user = session.get("gh_login")

    if login_disabled_for_user(user):
        g.user = None
        g.avatar = None
        return

    g.user = user
    g.avatar = session.get("gh_avatar")


@app.route("/github-callback")
@github.authorized_handler
def authorized(access_token):
    if access_token is None:
        return redirect(url_for("index"))

    state = request.args.get("state")
    if state is None or state != session.get("state"):
        return redirect(url_for("index"))
    session.pop("state")

    github_user = github.get("user", access_token=access_token)
    if github_user["type"] != "User":
        return redirect(url_for("faq", _anchor="signup"))

    if login_disabled_for_user(github_user["login"]):
        return render_template("time_error.jinja")

    # app.logger.debug(github_user)

    age = datetime.utcnow() - iso8601.parse_date(github_user["created_at"]).replace(
        tzinfo=None
    )

    app.logger.info(f"user is {age.days} days old")
    app.logger.info("user has {} followers".format(github_user["followers"]))
    if age.days < 31 and github_user["followers"] < 10:
        return redirect(url_for("faq", _anchor="signup"))

    session["gh_login"] = github_user["login"]
    if "redirect_after_login" in session:
        return redirect(session["redirect_after_login"])
    return redirect(url_for("dashboard"))


@app.route("/login")
def login():
    if g.user:
        return redirect(url_for("dashboard"))
    session["state"] = state = get_random()
    return github.authorize(state=state)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/")
def index():
    return render_template("index.jinja")


@app.route("/last")
def last():
    return render_template("last.jinja")


@app.route("/faq")
def faq():
    return render_template("faq.jinja")


if "INTERRUPT_KEY" in CONFIG:

    @app.route("/interrupt/{}".format(CONFIG["INTERRUPT_KEY"]))
    def saal():
        interrupt_key = get_scoped_api_key(
            [
                {
                    "Action": "device:node-message",
                    "Condition": {
                        "StringEquals": {"message:path": "root/remote/trigger"}
                    },
                    "Effect": "allow",
                }
            ],
            expire=900,
            uses=20,
        )
        return render_template(
            "interrupt.jinja",
            interrupt_key=interrupt_key,
        )


@app.route("/dashboard")
def dashboard():
    if not g.user:
        return redirect(url_for("index"))
    return render_template("dashboard.jinja")


@app.route("/content/list")
def content_list():
    if not g.user:
        session["redirect_after_login"] = request.url
        return redirect(url_for("login"))
    assets = get_user_assets()
    random.shuffle(assets)
    return jsonify(
        assets=assets,
    )


@app.route("/content/upload", methods=["POST"])
def content_upload():
    if not g.user:
        session["redirect_after_login"] = request.url
        return redirect(url_for("login"))

    if g.user.lower() not in CONFIG.get("ADMIN_USERS", set()):
        max_uploads = r.get(f"max_uploads:{g.user}")
        if max_uploads is not None:
            max_uploads = int(max_uploads)
        if not max_uploads:
            max_uploads = CONFIG["MAX_UPLOADS"]
        if len(get_user_assets()) >= max_uploads:
            return error("You have reached your upload limit")

    filetype = request.values.get("filetype")
    if filetype not in ("image", "video"):
        return error("Invalid/missing filetype")
    extension = "jpg" if filetype == "image" else "mp4"

    filename = "user/{}/{}_{}.{}".format(
        g.user, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), token_hex(8), extension
    )
    condition = {
        "StringEquals": {
            "asset:filename": filename,
            "asset:filetype": filetype,
            "userdata:user": g.user,
        },
        "NotExists": {
            "userdata:state": True,
        },
        "Boolean": {
            "asset:exists": False,
        },
    }
    if filetype == "image":
        condition.setdefault("NumericEquals", {}).update(
            {
                "asset:metadata:width": 1920,
                "asset:metadata:height": 1080,
            }
        )
        condition.setdefault("StringEquals", {}).update(
            {
                "asset:metadata:format": "jpeg",
            }
        )
    else:
        condition.setdefault("NumericLess", {}).update(
            {
                "asset:metadata:duration": 11,
            }
        )
        condition.setdefault("StringEquals", {}).update(
            {
                "asset:metadata:format": "h264",
            }
        )
    return jsonify(
        filename=filename,
        user=g.user,
        upload_key=get_scoped_api_key(
            [{"Action": "asset:upload", "Condition": condition, "Effect": "allow"}],
            uses=1,
        ),
    )


@app.route("/content/review/<int:asset_id>", methods=["POST"])
def content_request_review(asset_id):
    if not g.user:
        session["redirect_after_login"] = request.url
        return redirect(url_for("login"))

    try:
        asset = ib.get(f"asset/{asset_id}")
    except Exception:
        abort(404)

    if asset["userdata"].get("user") != g.user:
        return error("Cannot review")

    if "state" in asset["userdata"]:  # not in new state?
        return error("Cannot review")

    if g.user.lower() in CONFIG.get("ADMIN_USERS", set()):
        update_asset_userdata(asset, state="confirmed")
        app.logger.warn(
            "auto-confirming {} because it was uploaded by admin {}".format(
                asset["id"], g.user
            )
        )
        return jsonify(ok=True)

    moderation_url = url_for(
        "content_moderate", asset_id=asset_id, sig=mk_sig(asset_id), _external=True
    )

    client = mqtt.Client()
    if CONFIG.get("MQTT_USERNAME") and CONFIG.get("MQTT_PASSWORD"):
        client.username_pw_set(CONFIG["MQTT_USERNAME"], CONFIG["MQTT_PASSWORD"])
    client.connect(CONFIG["MQTT_SERVER"])
    result = client.publish(
        CONFIG["MQTT_TOPIC"],
        CONFIG["MQTT_MESSAGE"].format(
            user=g.user,
            asset=asset["filetype"].capitalize(),
            url=moderation_url,
        ),
    )
    client.disconnect()
    assert result[0] == 0

    app.logger.info("moderation url for {} is {}".format(asset["id"], moderation_url))

    update_asset_userdata(asset, state="review")
    return jsonify(ok=True)


@app.route("/content/moderate/<int:asset_id>-<sig>")
def content_moderate(asset_id, sig):
    if sig != mk_sig(asset_id):
        abort(404)
    if not g.user:
        session["redirect_after_login"] = request.url
        return redirect(url_for("login"))
    elif g.user.lower() not in CONFIG.get("ADMIN_USERS", set()):
        abort(401)

    try:
        asset = ib.get(f"asset/{asset_id}")
    except Exception:
        abort(404)

    state = asset["userdata"].get("state", "new")
    if state == "deleted":
        abort(404)

    return render_template(
        "moderate.jinja",
        asset={
            "id": asset["id"],
            "user": asset["userdata"]["user"],
            "filetype": asset["filetype"],
            "url": url_for("static", filename=cached_asset_name(asset)),
            "state": state,
        },
        sig=mk_sig(asset_id),
    )


@app.route(
    "/content/moderate/<int:asset_id>-<sig>/<any(confirm,reject):result>",
    methods=["POST"],
)
def content_moderate_result(asset_id, sig, result):
    if sig != mk_sig(asset_id):
        abort(404)
    if not g.user:
        session["redirect_after_login"] = request.url
        return redirect(url_for("login"))
    elif g.user.lower() not in CONFIG.get("ADMIN_USERS", set()):
        abort(401)

    try:
        asset = ib.get(f"asset/{asset_id}")
    except Exception:
        abort(404)

    if result == "confirm":
        app.logger.info("Asset {} was confirmed".format(asset["id"]))
        update_asset_userdata(asset, state="confirmed")
    else:
        app.logger.info("Asset {} was rejected".format(asset["id"]))
        update_asset_userdata(asset, state="rejected")

    return jsonify(ok=True)


@app.route("/content/<int:asset_id>", methods=["POST"])
def content_update(asset_id):
    if not g.user:
        session["redirect_after_login"] = request.url
        return redirect(url_for("login"))

    try:
        asset = ib.get(f"asset/{asset_id}")
    except Exception:
        abort(404)

    starts = request.values.get("starts", type=int)
    ends = request.values.get("ends", type=int)

    if asset["userdata"].get("user") != g.user:
        return error("Cannot update")

    try:
        update_asset_userdata(asset, starts=starts, ends=ends)
    except Exception as e:
        app.logger.error(f"content_update({asset_id}) {repr(e)}")
        return error("Cannot update")

    return jsonify(ok=True)


@app.route("/content/<int:asset_id>", methods=["DELETE"])
def content_delete(asset_id):
    if not g.user:
        session["redirect_after_login"] = request.url
        return redirect(url_for("login"))

    try:
        asset = ib.get(f"asset/{asset_id}")
    except Exception:
        abort(404)

    if asset["userdata"].get("user") != g.user:
        return error("Cannot delete")

    try:
        update_asset_userdata(asset, state="deleted")
    except Exception as e:
        app.logger.error(f"content_delete({asset_id}) {repr(e)}")
        return error("Cannot delete")

    return jsonify(ok=True)


@app.route("/content/live")
def content_live():
    no_time_filter = request.values.get("all")
    assets = get_all_live_assets(no_time_filter=no_time_filter)
    random.shuffle(assets)
    resp = jsonify(
        assets=[
            {
                "user": asset["userdata"]["user"],
                "filetype": asset["filetype"],
                "thumb": asset["thumb"],
                "url": url_for("static", filename=cached_asset_name(asset)),
            }
            for asset in assets
        ]
    )
    resp.headers["Cache-Control"] = "public, max-age=30"
    return resp


@app.route("/content/last")
def content_last():
    assets = get_all_live_assets()
    asset_by_id = dict((asset["id"], asset) for asset in assets)

    last = {}

    for room in CONFIG["ROOMS"]:
        proofs = [
            json.loads(data)
            for data in r.zrange("last:{}".format(room["device_id"]), 0, -1)
        ]

        last[room["name"]] = room_last = []
        for proof in reversed(proofs):
            asset = asset_by_id.get(proof["asset_id"])
            if asset is None:
                continue
            room_last.append(
                {
                    "id": proof["id"],
                    "user": asset["userdata"]["user"],
                    "filetype": asset["filetype"],
                    "shown": int(proof["ts"]),
                    "thumb": asset["thumb"],
                    "url": url_for("static", filename=cached_asset_name(asset)),
                }
            )
            if len(room_last) > 10:
                break

    resp = jsonify(
        last=[[room["name"], last.get(room["name"], [])] for room in CONFIG["ROOMS"]]
    )
    resp.headers["Cache-Control"] = "public, max-age=5"
    return resp


@app.route("/proof", methods=["POST"])
def proof():
    proofs = [(json.loads(row), row) for row in request.stream.read().split("\n")]
    device_ids = set()
    p = r.pipeline()
    for proof, row in proofs:
        p.zadd("last:{}".format(proof["device_id"]), row, proof["ts"])
        device_ids.add(proof["device_id"])
    for device_id in device_ids:
        p.zremrangebyscore(f"last:{device_id}", 0, time.time() - 1200)
    p.execute()
    return "ok"


@app.route("/robots.txt")
def robots_txt():
    return "User-Agent: *\nDisallow: /\n"


if __name__ == "__main__":
    app.run(port=8080)
