from core.logging_setup import setup_logging
from gui.main_window import MainWindow
import logging


def main():
    setup_logging()
    log = logging.getLogger(__name__)

    # ダミーの開始/停止コールバック
    def on_start(username, password, otp, interval):
        log.info("Start requested: user=%s interval=%s", username, interval)
        # ここで WatcherThread を作って .attach_event_queue(queue) で注入してね

    def on_stop():
        log.info("Stop requested")

    app = MainWindow()
    app.mainloop()


if __name__ == "__main__":
    main()
