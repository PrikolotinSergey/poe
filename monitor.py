import hashlib
import json
import os
import re
import sys
import warnings
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from requests.packages.urllib3.exceptions import InsecureRequestWarning
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


SOURCE_URL = (
    "https://www.poe.pl.ua/customs/dynamicgpv-info.php"
)
PAGE_URL = "https://prikolotinsergey.github.io/poe/"
STATE_FILE = Path(os.environ.get("STATE_FILE", "monitor_state.json"))
KYIV = ZoneInfo("Europe/Kyiv")

# Соответствует очередям, которые показывает index.html:
# индекс строки 0 -> очередь 1.1
# индекс строки 4 -> очередь 3.1
PLANS = [(0, "1.1"), (4, "3.1")]

STATUS = {
    "1": "Есть свет",
    "2": "Нет света",
    "3": "Возможны отключения",
}


def make_insecure_session():
    """Создать Session с ретраями и (временно) без проверки сертификатов.

    Это обходная мера: отключаем проверку TLS (verify=False) и подавляем
    InsecureRequestWarning, потому что целевой сервер возвращает некорректную
    информацию об отзыве сертификата (OCSP/CRL). Использовать только для
    публичного HTML, не для передачи секретов.
    """
    # Подавляем предупреждение о небезопасном соединении — оно информативно,
    # но загромождает логи в CI.
    warnings.simplefilter("ignore", InsecureRequestWarning)

    session = requests.Session()
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=None,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def fetch_html():
    """Получить HTML тем же POST-запросом, что использует сайт.

    Временно выполняем запрос с verify=False и ретраями, чтобы обойти
    проблемы с проверкой отзыва сертификата на стороне сервера/CA.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; POEMonitor/1.0)",
        "Referer": PAGE_URL,
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    session = make_insecure_session()

    try:
        response = session.post(
            SOURCE_URL,
            headers=headers,
            data={},
            timeout=(20, 50),
            verify=False,
        )
    except requests.exceptions.RequestException as exc:
        print(f"Ошибка запроса к {SOURCE_URL}: {exc}", file=sys.stderr)
        raise

    try:
        response.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        body_preview = (
            (response.text[:1000] + "...") if response.text else "<empty>"
        )
        print(
            f"HTTP error {response.status_code} from {SOURCE_URL}: {exc}",
            file=sys.stderr,
        )
        print("Response body preview:", body_preview, file=sys.stderr)
        raise

    if not response.text.strip():
        print("Пустой ответ от источника:", file=sys.stderr)
        print("Status:", response.status_code, file=sys.stderr)
        print("Headers:", response.headers, file=sys.stderr)
        print("Body length:", len(response.text), file=sys.stderr)
        raise RuntimeError("Источник вернул пустой ответ")

    return response.text


def extract_mask(row):
    """Преобразовать 48 получасовых ячеек в строку состояний."""
    mask = []

    for td in row.select("td"):
        classes = td.get("class", [])
        light_class = next(
            (
                cls for cls in classes
                if re.fullmatch(r"light_[123]", cls)
            ),
            None,
        )

        # В исходном коде учитываются только light_* ячейки.
        if light_class is None:
            continue

        mask.append(light_class[-1])

    if len(mask) != 48:
        raise RuntimeError(
            f"Ожидалось 48 получасовых ячеек, получено {len(mask)}"
        )

    return "".join(mask)


def format_time(slot):
    """Номер получасового интервала -> время."""
    return f"{slot // 2:02d}:{'30' if slot % 2 else '00'}"


def format_schedule(mask):
    """Сгруппировать соседние интервалы с одинаковым состоянием."""
    intervals = []
    start = 0

    for i in range(1, len(mask) + 1):
        if i == len(mask) or mask[i] != mask[start]:
            intervals.append(
                f"{format_time(start)}–{format_time(i)}: "
                f"{STATUS[mask[start]]}"
            )
            start = i

    return "\n".join(intervals)


def get_schedule():
    """Извлечь расписание, которое показывает текущая страница."""
    html = fetch_html()
    soup = BeautifulSoup(html, "html.parser")
    containers = soup.select("div.gpvinfodetail")

    if not containers:
        raise RuntimeError(
            "Не найдены div.gpvinfodetail. "
            "Структура ответа могла измениться."
        )

    today = datetime.now(KYIV).date()
    result = []

    for day_index, container in enumerate(containers):
        rows = container.select("tbody > tr")
        day = today + timedelta(days=day_index)

        for row_index, queue_name in PLANS:
            if row_index >= len(rows):
                raise RuntimeError(
                    f"Для {day} нет строки {row_index} "
                    f"(очередь {queue_name})"
                )

            mask = extract_mask(rows[row_index])
            result.append({
                "date": day.isoformat(),
                "queue": queue_name,
                "mask": mask,
                "intervals": format_schedule(mask),
            })

    if not result:
        raise RuntimeError("Расписание не удалось извлечь")

    return result


def send_telegram(text):
    """Отправить сообщение, разбив длинный текст на части."""
    token = os.environ["BOT_TOKEN"]
    chat_id = os.environ["CHAT_ID"]

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    # Оставляем запас относительно лимита Telegram в 4096 символов.
    chunks = []
    current = ""

    for line in text.splitlines():
        if len(line) > 3800:
            raise RuntimeError("Одна строка сообщения слишком длинная")

        candidate = f"{current}\n{line}" if current else line

        if len(candidate) > 3800:
            chunks.append(current)
            current = line
        else:
            current = candidate

    if current:
        chunks.append(current)

    with requests.Session() as session:
        for chunk in chunks:
            response = session.post(
                url,
                json={"chat_id": chat_id, "text": chunk},
                timeout=20,
            )
            response.raise_for_status()

            payload = response.json()
            if not payload.get("ok"):
                raise RuntimeError(
                    f"Telegram API вернул ошибку: {payload}"
                )


def render_message(schedule):
    now = datetime.now(KYIV).strftime("%d.%m.%Y %H:%M")
    lines = [
        "⚡ Обновилось расписание отключений",
        f"Проверено: {now} (Киев)",
        "",
    ]

    for item in schedule:
        date = datetime.fromisoformat(item["date"]).strftime("%d.%m.%Y")
        lines.append(f"Очередь {item['queue']} — {date}")
        lines.append(item["intervals"])
        lines.append("")

    lines.append(f"Источник: {PAGE_URL}")
    return "\n".join(lines).strip()


def main():
    schedule = get_schedule()

    # Сравниваем только значимые данные: даты, очереди и состояния.
    snapshot = json.dumps(
        schedule,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(snapshot.encode("utf-8")).hexdigest()

    old_digest = None
    if STATE_FILE.exists():
        try:
            old_state = json.loads(STATE_FILE.read_text("utf-8"))
            old_digest = old_state.get("digest")
        except (OSError, json.JSONDecodeError):
            raise RuntimeError(
                "Файл состояния повреждён. "
                "Не отправляю уведомление и не перезаписываю его."
            )

    # Первый запуск: фиксируем исходное расписание без уведомления.
    if old_digest is None:
        save_state(digest, schedule)
        print("Создана начальная точка отсчёта.")
        return

    if digest == old_digest:
        print("Изменений нет.")
        return

    # Сначала отправляем уведомление. При ошибке состояние не обновляется.
    send_telegram(render_message(schedule))
    save_state(digest, schedule)
    print("Изменения отправлены в Telegram.")


def save_state(digest, schedule):
    data = {
        "digest": digest,
        "schedule": schedule,
        "checked_at": datetime.now(KYIV).isoformat(),
    }
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ОШИБКА МОНИТОРИНГА: {exc}", file=sys.stderr)
        raise
