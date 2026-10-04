import datetime


def notify(text, kind="info", source=None):
    timestamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    source_label = f" [{source}]" if source else ""
    print(f"[{timestamp}] [{kind}]{source_label} {text}")
