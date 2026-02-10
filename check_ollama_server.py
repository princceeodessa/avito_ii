import requests
import json

print("🔍 Проверяю сервер Ollama через API...")

try:
    # Пробуем получить список моделей через API
    response = requests.get('http://localhost:11434/api/tags', timeout=5)

    if response.status_code == 200:
        data = response.json()
        models = data.get('models', [])

        if models:
            print(f"✅ Сервер Ollama работает! Моделей: {len(models)}")
            for model in models:
                print(f"   - {model['name']}")

            # Выводим полный ответ для отладки
            print("\n📋 Полный ответ сервера:")
            print(json.dumps(data, indent=2, ensure_ascii=False))
        else:
            print("⚠️  Сервер работает, но модели не загружены")
            print("Полный ответ:", json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print(f"❌ Сервер ответил ошибкой: {response.status_code}")
        print("Текст ответа:", response.text)

except requests.ConnectionError:
    print("❌ Не удалось подключиться к серверу Ollama")
    print("Вероятно, сервер не запущен или блокируется")
except Exception as e:
    print(f"❌ Неожиданная ошибка: {e}")