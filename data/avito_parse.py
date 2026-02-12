import re
import json

# Файл с выгрузкой
INPUT_FILE = "avito_dump.txt"
OUTPUT_FILE = "dialogs.json"

# Регулярки для извлечения дат, телефонов, адресов
phone_pattern = re.compile(r'\+?\d[\d\s-]{7,}\d')
date_pattern = re.compile(r'\d{1,2}\s\w+\s\d{4}')
address_pattern = re.compile(r'([А-Яа-яЁёA-Za-z0-9\s.,-]+)')

def parse_dialog(dialog_text):
    """Парсим один диалог"""
    dialog = {
        "client_form": None,
        "messages": [],
        "phones": [],
        "addresses": [],
        "dates": [],
        "comment": None
    }

    lines = [line.strip() for line in dialog_text.split("\n") if line.strip()]

    # Вытаскиваем комментарий (если есть)
    comment_lines = [line for line in lines if line.lower().startswith("комментарий")]
    if comment_lines:
        dialog["comment"] = " ".join(comment_lines).split(":", 1)[-1].strip()

    # Вытаскиваем анкету
    if "АНКЕТА ОТ КЛИЕНТА" in dialog_text:
        form_lines = []
        capture = False
        for line in lines:
            if line.startswith("АНКЕТА ОТ КЛИЕНТА"):
                capture = True
                continue
            if line.startswith("КЦ:") or line.startswith("К:"):
                capture = False
            if capture:
                form_lines.append(line)
        if form_lines:
            dialog["client_form"] = "\n".join(form_lines).strip()

    # Вытаскиваем сообщения
    for line in lines:
        if line.startswith("КЦ:"):
            dialog["messages"].append({"role": "agent", "text": line[3:].strip()})
        elif line.startswith("К:"):
            dialog["messages"].append({"role": "client", "text": line[2:].strip()})

    # Вытаскиваем телефоны, даты и адреса
    all_text = "\n".join(lines)
    dialog["phones"] = phone_pattern.findall(all_text)
    dialog["dates"] = date_pattern.findall(all_text)
    # Простейшая попытка найти адреса
    dialog["addresses"] = [line for line in lines if any(word in line.lower() for word in ["ул.", "д.", "кв.", "п.", "район", "поселок"])]

    return dialog

def main():
    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        text = f.read()

    # Разделяем по диалогам
    raw_dialogs = re.split(r"ДИАЛОГ\s+\d+", text, flags=re.IGNORECASE)
    dialogs = []

    for raw in raw_dialogs:
        raw = raw.strip()
        if not raw:
            continue
        dialogs.append(parse_dialog(raw))

    # Сохраняем JSON
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(dialogs, f, ensure_ascii=False, indent=4)

    print(f"✅ Обработано {len(dialogs)} диалогов. Сохранено в {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
