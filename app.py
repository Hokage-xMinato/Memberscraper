import os
from flask import Flask, request, jsonify, send_from_directory
from telethon.sync import TelegramClient
from telethon.tl.functions.messages import GetDialogsRequest
from telethon.tl.types import InputPeerEmpty, InputPeerChannel, InputPeerUser
from telethon.errors.rpcerrorlist import PeerFloodError, UserPrivacyRestrictedError, SessionPasswordNeededError
from telethon.tl.functions.channels import InviteToChannelRequest
import csv
import io
import traceback
import time
import re
import threading
import json

# Firebase Imports
# This block attempts to import Firebase Admin SDK.
# If it's not installed, it will fall back to a mode without Firestore persistence.
try:
    import firebase_admin
    from firebase_admin import credentials, firestore
    FIREBASE_AVAILABLE = True
except ImportError:
    FIREBASE_AVAILABLE = False
    print("Firebase Admin SDK not installed. Session persistence via Firestore will not work.")
    print("Please install with: pip install firebase-admin")


app = Flask(__name__, static_folder='.', static_url_path='') # Serve static files from current directory

# Global client and session state (managed per user via Firestore or in-memory)
_telegram_clients = {}
_telegram_client_locks = {} # To prevent race conditions if multiple requests for same user happen simultaneously

db_backend = None # Firestore client instance for the backend

# --- Firebase Initialization in Backend ---
def initialize_firebase_backend(firebase_config_dict):
    global db_backend
    if not FIREBASE_AVAILABLE:
        print("Firebase Admin SDK not available. Cannot initialize Firebase backend.")
        return None

    try:
        if not firebase_admin._apps: # Initialize only once if not already initialized
            if firebase_config_dict:
                # Use the provided dictionary as credentials
                cred = credentials.Certificate(firebase_config_dict)
                firebase_admin.initialize_app(cred)
                print("Firebase backend initialized.")
            else:
                print("Firebase config is empty. Firestore will not be available in backend.")
        db_backend = firestore.client()
        return db_backend
    except Exception as e:
        print(f"Error initializing Firebase backend: {e}")
        return None

# --- Helper to get or create a TelethonClient for a user ---
def get_user_client(user_id, api_id, api_hash, string_session=None):
    if user_id not in _telegram_clients:
        # Telethon session name needs to be unique per user.
        # This will create a session file if not using a string_session,
        # but on Render's free tier, these files are ephemeral.
        if string_session:
            client = TelegramClient(string_session, api_id, api_hash)
        else:
            client = TelegramClient(f'user_session_{user_id}', api_id, api_hash)
        _telegram_clients[user_id] = client
        _telegram_client_locks[user_id] = threading.Lock()
    return _telegram_clients[user_id]

# --- Flask Routes ---

@app.route('/')
def serve_index():
    """Serves the index.html file from the root directory."""
    return send_from_directory('.', 'index.html')

@app.route('/send_code', methods=['POST'])
def send_code_route():
    """
    Handles sending the verification code to the user's phone.
    Requires api_id, api_hash, and phone number.
    """
    data = request.json
    api_id = data.get('api_id')
    api_hash = data.get('api_hash')
    phone = data.get('phone')
    user_id = data.get('userId')

    if not all([api_id, api_hash, phone, user_id]):
        return jsonify({"error": "Missing API ID, API Hash, phone, or userId"}), 400

    firebase_config_dict = data.get('firebaseConfig', {})
    global db_backend
    if FIREBASE_AVAILABLE and not db_backend:
        db_backend = initialize_firebase_backend(firebase_config_dict)
        if not db_backend and firebase_config_dict: # If config was provided but init failed
            return jsonify({"error": "Firebase backend initialization failed. Cannot save/load session."}), 500

    try:
        client = get_user_client(user_id, api_id, api_hash)
        with _telegram_client_locks[user_id]: # Acquire lock for client operation
            if not client.is_connected():
                client.connect()
            if not client.is_user_authorized():
                # Store client details temporarily to recall during sign_in
                client.api_id = api_id # Store on the client instance directly
                client.api_hash = api_hash
                client.phone = phone
                client.send_code_request(phone)
                return jsonify({"message": "Verification code sent. Please enter it on the next step."})
            else:
                return jsonify({"message": "Already authorized.", "string_session": client.session.save()})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route('/sign_in', methods=['POST'])
def sign_in_route():
    """
    Handles signing in with the verification code.
    Requires phone number, phone code, api_id, api_hash.
    """
    data = request.json
    phone = data.get('phone')
    phone_code = data.get('phone_code')
    api_id = data.get('api_id')
    api_hash = data.get('api_hash')
    user_id = data.get('userId')
    firebase_config_dict = data.get('firebaseConfig', {})

    if not all([phone, phone_code, user_id, api_id, api_hash]):
        return jsonify({"error": "Missing phone, phone code, API ID, API Hash, or userId"}), 400

    global db_backend
    if FIREBASE_AVAILABLE and not db_backend:
        db_backend = initialize_firebase_backend(firebase_config_dict)
        if not db_backend and firebase_config_dict:
            return jsonify({"error": "Firebase backend initialization failed. Cannot save/load session."}), 500

    client = _telegram_clients.get(user_id)
    if not client or not hasattr(client, 'phone') or client.phone != phone:
        # This case means the backend restarted or client was not fully set up
        client = get_user_client(user_id, api_id, api_hash)
        try:
            client.connect()
        except Exception as e:
            return jsonify({"error": f"Failed to connect client for sign-in: {e}"}), 500
        client.phone = phone # Set phone on client for sign_in

    try:
        with _telegram_client_locks[user_id]:
            client.sign_in(phone, phone_code)
            string_session = client.session.save()
            
            # Save session string and API credentials to Firestore if available
            if FIREBASE_AVAILABLE and db_backend:
                doc_ref = db_backend.collection(f'artifacts/{os.environ.get("RENDER_EXTERNAL_HOSTNAME", "default-app-id")}/users/{user_id}/telegram_sessions').document('user_session')
                doc_ref.set({
                    'string_session': string_session,
                    'phone_number': phone,
                    'api_id': api_id,
                    'api_hash': api_hash,
                    'last_auth_time': firestore.SERVER_TIMESTAMP
                })
            return jsonify({"message": "Signed in successfully!", "string_session": string_session})
    except SessionPasswordNeededError:
        return jsonify({"error": "Two-factor authentication is enabled. Please handle password if needed."}), 403
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route('/connect', methods=['POST'])
def connect_route():
    """
    Connects the Telethon client using a provided string_session and API credentials.
    This is used to re-establish connection without re-authenticating.
    """
    data = request.json
    string_session = data.get('string_session')
    api_id = data.get('api_id')
    api_hash = data.get('api_hash')
    phone_number = data.get('phone_number')
    user_id = data.get('userId')
    firebase_config_dict = data.get('firebaseConfig', {})

    if not all([string_session, api_id, api_hash, user_id]):
        return jsonify({"error": "Missing string session, API ID, API Hash, or userId"}), 400

    global db_backend
    if FIREBASE_AVAILABLE and not db_backend:
        db_backend = initialize_firebase_backend(firebase_config_dict)
        if not db_backend and firebase_config_dict:
            return jsonify({"error": "Firebase backend initialization failed. Cannot save/load session."}), 500

    try:
        client = get_user_client(user_id, api_id, api_hash, string_session)
        with _telegram_client_locks[user_id]:
            if not client.is_connected():
                client.connect()
            if not client.is_user_authorized():
                # If connect() succeeded but not authorized, the session might be invalid.
                if user_id in _telegram_clients:
                    _telegram_clients[user_id].disconnect() # Disconnect and remove bad session
                    del _telegram_clients[user_id]
                    del _telegram_client_locks[user_id]
                return jsonify({"error": "Client not authorized with the provided session. Requires re-authentication."}), 401
            return jsonify({"message": "Client connected and authorized.", "phone_number": phone_number})
    except Exception as e:
        traceback.print_exc()
        # If connection fails, remove client from cache to ensure clean retry
        if user_id in _telegram_clients:
            _telegram_clients[user_id].disconnect()
            del _telegram_clients[user_id]
            del _telegram_client_locks[user_id]
        return jsonify({"error": str(e)}), 500


@app.route('/get_groups', methods=['POST'])
def get_groups_route():
    """Lists available megagroups for the authenticated user."""
    data = request.json
    user_id = data.get('userId')

    client = _telegram_clients.get(user_id)
    if not client or not client.is_connected() or not client.is_user_authorized():
        return jsonify({"error": "Telegram client not connected or authorized. Please authenticate."}), 401

    try:
        with _telegram_client_locks[user_id]:
            result = client(GetDialogsRequest(
                offset_date=None,
                offset_id=0,
                offset_peer=InputPeerEmpty(),
                limit=200,
                hash=0
            ))
            groups = [
                {'id': chat.id, 'title': chat.title, 'access_hash': chat.access_hash}
                for chat in result.chats if getattr(chat, 'megagroup', False)
            ]
            return jsonify({"groups": groups})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route('/list_members', methods=['POST'])
def list_members_route():
    """Lists members of a selected group and returns them."""
    data = request.json
    group_id = data.get('group_id')
    group_hash = data.get('group_hash')
    user_id = data.get('userId')

    if not all([group_id, group_hash, user_id]):
        return jsonify({"error": "Missing group ID, hash, or userId"}), 400

    client = _telegram_clients.get(user_id)
    if not client or not client.is_connected() or not client.is_user_authorized():
        return jsonify({"error": "Telegram client not connected or authorized. Please authenticate."}), 401

    try:
        with _telegram_client_locks[user_id]:
            target_group_entity = InputPeerChannel(group_id, group_hash)
            participants = client.get_participants(target_group_entity, aggressive=True)
            
            members_data = []
            for user in participants:
                name = ' '.join(filter(None, [user.first_name, user.last_name]))
                members_data.append({
                    'username': user.username,
                    'user_id': user.id,
                    'access_hash': user.access_hash,
                    'name': name
                })
            return jsonify({"members": members_data})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route('/add_members', methods=['POST'])
def add_members_route():
    """Adds users from provided CSV data to a selected group."""
    data = request.json
    group_id = data.get('group_id')
    group_hash = data.get('group_hash')
    add_method = data.get('add_method')
    csv_data = data.get('csv_data')
    user_id = data.get('userId')

    if not all([group_id, group_hash, add_method, csv_data, user_id]):
        return jsonify({"error": "Missing group ID, hash, add method, CSV data, or userId"}), 400

    client = _telegram_clients.get(user_id)
    if not client or not client.is_connected() or not client.is_user_authorized():
        return jsonify({"error": "Telegram client not connected or authorized. Please authenticate."}), 401

    users_to_add = []
    csv_file = io.StringIO(csv_data)
    reader = csv.reader(csv_file, delimiter=',', lineterminator='\n')
    next(reader)  # Skip header
    for row in reader:
        if len(row) < 3:
            print(f'Skipping incomplete user record: {row}')
            continue
        users_to_add.append({
            'username': row[0],
            'id': int(row[1]) if row[1] else 0,
            'access_hash': int(row[2]) if row[2] else 0
        })

    def _add_members_threaded(client_instance, target_group_entity, users, method, user_id_for_logging):
        added_count = 0
        skipped_count = 0
        errors = []
        with _telegram_client_locks[user_id_for_logging]:
            for user in users:
                try:
                    print(f'[{user_id_for_logging}] Attempting to add {user["username"] or user["id"]}')
                    user_entity = None
                    if method == 1:  # by username
                        if not user['username']:
                            errors.append(f'Skipping user {user["id"]} due to missing username for method 1.')
                            skipped_count += 1
                            continue
                        user_entity = client_instance.get_input_entity(user['username'])
                    elif method == 2:  # by ID
                        if not user['id'] or not user['access_hash']:
                            errors.append(f'Skipping user {user["username"]} due to missing ID/Access Hash for method 2.')
                            skipped_count += 1
                            continue
                        user_entity = InputPeerUser(user['id'], user['access_hash'])
                    else:
                        errors.append(f'Invalid add method specified for user {user["username"] or user["id"]}.')
                        break

                    if user_entity:
                        client_instance(InviteToChannelRequest(target_group_entity, [user_entity]))
                        added_count += 1
                        print(f'[{user_id_for_logging}] Successfully added {user["username"] or user["id"]}. Waiting 60 seconds...')
                        time.sleep(60)
                except PeerFloodError:
                    errors.append('PeerFloodError: Too many requests. Stopping addition.')
                    print(f'[{user_id_for_logging}] Flood error. Stopping.')
                    break
                except UserPrivacyRestrictedError:
                    skipped_count += 1
                    errors.append(f'UserPrivacyRestrictedError for {user["username"] or user["id"]}: Privacy restrictions. Skipping.')
                    print(f'Privacy restrictions for {user["username"] or user["id"]}. Skipping.')
                except Exception as e:
                    skipped_count += 1
                    errors.append(f'Error adding {user["username"] or user["id"]}: {str(e)}')
                    traceback.print_exc()

            final_message = (f'[{user_id_for_logging}] Finished adding members. '
                             f'Added: {added_count}, Skipped: {skipped_count}. '
                             f'Total users processed: {len(users)}. '
                             f'Errors: {"; ".join(errors) if errors else "None."}')
            print(final_message)

    target_group_entity = InputPeerChannel(group_id, group_hash)
    
    add_thread = threading.Thread(target=_add_members_threaded, args=(client, target_group_entity, users_to_add, add_method, user_id))
    add_thread.start()

    return jsonify({"message": f"Adding members initiated for {len(users_to_add)} users. Progress will be logged on the server. Please allow time for completion due to Telegram's rate limits (60s per user)."}), 202

if __name__ == '__main__':
    # When running locally, Flask defaults to port 5000.
    # On Render, the port is provided via the PORT environment variable.
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
