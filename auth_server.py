from flask import Flask, request
import time

app = Flask(__name__)
auth_code = None


@app.route("/callback")
def callback():
    global auth_code
    auth_code = request.args.get("code")
    return "✅ Авторизация Avito успешна. Можешь закрыть вкладку."


def run_server():
    app.run(
        host="0.0.0.0",
        port=8765,
        debug=False,
        use_reloader=False
    )


def wait_for_code(timeout=300):
    global auth_code
    start = time.time()

    while auth_code is None:
        if time.time() - start > timeout:
            raise TimeoutError("⏰ Не получен code от Avito")
        time.sleep(0.5)

    return auth_code
