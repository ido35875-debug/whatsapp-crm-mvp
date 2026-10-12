import requests

url = "https://whatsapp-crm-pilot.onrender.com/webhook"
payload = {
    "typeWebhook": "incomingMessageReceived",
    "senderData": {"chatId": "972500002029@c.us", "senderName": "בדיקה"},
    "messageData": {
        "typeMessage": "textMessage",
        "textMessageData": {"textMessage": "היי"}
    }
}

response = requests.post(url, json=payload)
print("Status Code:", response.status_code)
print("Response:", response.text)