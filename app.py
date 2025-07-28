import os
from flask import Flask, request
import requests
from pymongo import MongoClient
from bson.objectid import ObjectId

# ===============================================================
# 1. INITIALIZE THE APP AND LOAD CONFIGURATION
# ===============================================================
app = Flask(__name__)

# These values will be set in your Render environment, not here.
META_ACCESS_TOKEN = os.environ.get('META_ACCESS_TOKEN')
META_VERIFY_TOKEN = os.environ.get('META_VERIFY_TOKEN')
MONGO_CONNECTION_STRING = os.environ.get('MONGO_CONNECTION_STRING')
API_URL = f"https://graph.facebook.com/v19.0/me/messages?access_token={META_ACCESS_TOKEN}"

# ===============================================================
# 2. SETUP DATABASE CONNECTION
# ===============================================================
try:
    client = MongoClient(MONGO_CONNECTION_STRING)
    db = client.exam_bot_db # You can name your database anything you like
    print("✅ Successfully connected to MongoDB.")
except Exception as e:
    print(f"❌ Error connecting to MongoDB: {e}")
    # The app will likely fail to start if this happens, which is what we want.

# ===============================================================
# 3. HELPER FUNCTIONS (The bot's tools)
# ===============================================================

def send_message(recipient_id, message_payload):
    """Sends a message to a specific user on Instagram."""
    payload = {
        'recipient': {'id': recipient_id},
        'messaging_type': 'RESPONSE',
        'message': message_payload
    }
    try:
        requests.post(API_URL, json=payload)
    except requests.exceptions.RequestException as e:
        print(f"❌ Error sending message: {e}")

def fetch_unseen_question(user_id, exam_name):
    """
    Finds a random question for the user from the specified exam
    that they have not seen before.
    """
    user_doc = db.users.find_one({"_id": user_id})
    seen_ids = user_doc['seen_question_ids'] if user_doc and 'seen_question_ids' in user_doc else []
    
    # Convert string IDs from our DB into MongoDB's native ObjectId format for querying
    seen_object_ids = [ObjectId(id_str) for id_str in seen_ids]

    # The Aggregation Pipeline is a powerful MongoDB feature to perform complex queries.
    pipeline = [
        # Stage 1: Filter for questions in the right exam and NOT in the user's 'seen' list.
        {'$match': {'exam_name': exam_name, '_id': {'$nin': seen_object_ids}}},
        # Stage 2: Select 1 random document from the filtered results.
        {'$sample': {'size': 1}}
    ]
    
    result = list(db.questions.aggregate(pipeline))
    return result[0] if result else None

def mark_question_as_seen(user_id, question_id):
    """
    Adds a question's ID to the user's list of seen questions.
    Creates the user document if it's their first time.
    """
    db.users.update_one(
        {'_id': user_id},
        {'$push': {'seen_question_ids': str(question_id)}}, # We store the ID as a string
        upsert=True
    )

def get_question_by_id(question_id):
    """Fetches a specific question document by its ID."""
    return db.questions.find_one({"_id": ObjectId(question_id)})

# ===============================================================
# 4. WEBHOOK (The bot's "ears" and "mouth")
# ===============================================================

@app.route('/webhook', methods=['GET', 'POST'])
def webhook():
    # --- Webhook Verification (for setup only) ---
    if request.method == 'GET':
        if request.args.get("hub.verify_token") == META_VERIFY_TOKEN:
            return request.args.get("hub.challenge")
        return "Verification token mismatch", 403

    # --- Handle Incoming Messages ---
    if request.method == 'POST':
        data = request.get_json()
        if data.get('object') == 'instagram':
            for entry in data.get('entry', []):
                for event in entry.get('messaging', []):
                    sender_id = event['sender']['id']
                    
                    # --- Case 1: User sends a text message ---
                    if event.get('message') and event['message'].get('text'):
                        message_text = event['message']['text'].upper()
                        exam_name = None
                        
                        # Check for keywords to start a quiz
                        if "NEET" in message_text or "NEXT" in message_text:
                            exam_name = "NEET"
                        elif "JEE" in message_text:
                            exam_name = "JEE"

                        if exam_name:
                            handle_new_question_request(sender_id, exam_name)
                        else:
                            # Default reply for unknown commands
                            send_message(sender_id, {"text": "Hello! Type 'NEET' or 'JEE' to start a quiz."})
                            
                    # --- Case 2: User clicks a button (a 'postback') ---
                    elif event.get('postback'):
                        payload = event['postback']['payload']
                        handle_postback(sender_id, payload)
    
    return "OK", 200


def handle_new_question_request(sender_id, exam_name):
    """Handles the logic for fetching and sending a new question."""
    question_doc = fetch_unseen_question(sender_id, exam_name)
    
    if question_doc:
        q_id = question_doc['_id']
        q_text = question_doc['question_text']
        opts = question_doc['options']
        
        # Immediately mark the question as seen to prevent repeats
        mark_question_as_seen(sender_id, q_id)

        buttons = [
            {"type": "postback", "title": "A", "payload": f"ANSWER_{q_id}_a"},
            {"type": "postback", "title": "B", "payload": f"ANSWER_{q_id}_b"},
            {"type": "postback", "title": "C", "payload": f"ANSWER_{q_id}_c"},
            {"type": "postback", "title": "D", "payload": f"ANSWER_{q_id}_d"},
        ]
        
        question_to_send = f"{q_text}\n\nA) {opts['a']}\nB) {opts['b']}\nC) {opts['c']}\nD) {opts['d']}"
        send_message(sender_id, {"text": question_to_send, "quick_replies": buttons})
    else:
        # User has finished all questions for this exam
        send_message(sender_id, {"text": f"🎉 Congratulations! You've completed all available questions for {exam_name}."})

def handle_postback(sender_id, payload):
    """Handles all button clicks from the user."""
    # --- Logic for handling an answer ---
    if payload.startswith("ANSWER_"):
        parts = payload.split('_')
        question_id = parts[1]
        user_answer = parts[2]
        
        question_doc = get_question_by_id(question_id)
        if question_doc:
            correct_answer = question_doc['correct_option']
            explanation = question_doc.get('explanation', 'No explanation available.')

            if user_answer == correct_answer:
                reply_text = f"✅ Correct!\n\n{explanation}"
            else:
                reply_text = f"❌ Incorrect. The correct answer was ({correct_answer.upper()}).\n\n{explanation}"
            
            # Send the result and a button to get the next question
            next_exam = question_doc['exam_name'] # Get the exam name from the question itself
            send_message(sender_id, {
                "text": reply_text,
                "quick_replies": [
                    {"type": "postback", "title": "Next Question", "payload": f"NEXT_{next_exam}"}
                ]
            })

    # --- Logic for the "Next Question" button ---
    elif payload.startswith("NEXT_"):
        exam_name = payload.split('_')[1]
        handle_new_question_request(sender_id, exam_name)

# ===============================================================
# 5. START THE SERVER
# ===============================================================
if __name__ == '__main__':
    # This part is for local testing. Render will use Gunicorn to run the app.
    app.run(debug=True)