from flask import Flask, render_template, send_from_directory
from flask_socketio import SocketIO, join_room, leave_room, emit
import os

app = Flask(
    __name__,
    template_folder=os.path.join(os.path.dirname(__file__), '..', 'frontend'),
    static_folder=os.path.join(os.path.dirname(__file__), '..', 'frontend')
)
app.config['SECRET_KEY'] = 'webrtc-secret-key-change-in-production'

socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# Track who is in each room: { room_id: [socket_id, ...] }
rooms = {}

# http routes
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/room/<room_id>')
def room(room_id):
    return render_template('room.html', room_id=room_id)


#Socket.IO Events

@socketio.on('join')
def handle_join(data):
    from flask_socketio import request
    room_id = data['room']
    join_room(room_id)

    if room_id not in rooms:
        rooms[room_id] = []

    # Tell the new peer who else is already in the room
    existing_peers = rooms[room_id].copy()
    emit('room_peers', {'peers': existing_peers})

    # Tell everyone else that a new peer arrived
    emit('peer_joined', {'peerId': request.sid}, to=room_id, skip_sid=request.sid)

    rooms[room_id].append(request.sid)
    print(f"[JOIN] {request.sid} joined room '{room_id}' | peers now: {rooms[room_id]}")


@socketio.on('offer')
def handle_offer(data):
    from flask_socketio import request
    target = data['target']
    emit('offer', {
        'sdp': data['sdp'],
        'caller': request.sid
    }, to=target)
    print(f"[OFFER] {request.sid} → {target}")


@socketio.on('answer')
def handle_answer(data):
    from flask_socketio import request
    target = data['target']
    emit('answer', {
        'sdp': data['sdp'],
        'answerer': request.sid
    }, to=target)
    print(f"[ANSWER] {request.sid} → {target}")


@socketio.on('ice_candidate')
def handle_ice_candidate(data):
    from flask_socketio import request
    target = data['target']
    emit('ice_candidate', {
        'candidate': data['candidate'],
        'sender': request.sid
    }, to=target)


@socketio.on('disconnect')
def handle_disconnect():
    from flask_socketio import request
    sid = request.sid
    for room_id, peers in rooms.items():
        if sid in peers:
            peers.remove(sid)
            emit('peer_left', {'peerId': sid}, to=room_id)
            print(f"[LEAVE] {sid} left room '{room_id}' | peers now: {peers}")
            break


@socketio.on('chat_message')
def handle_chat(data):
    from flask_socketio import request
    room_id = data['room']
    # skip_sid=request.sid prevents the sender receiving their own message
    # back as an 'other' bubble (they already appended it locally as 'You')
    emit('chat_message', {
        'message': data['message'],
        'sender': data.get('name', 'Anonymous'),
        'senderId': request.sid
    }, to=room_id, skip_sid=request.sid)


if __name__ == '__main__':
    print("🚀 WebRTC Signaling Server running on http://localhost:5000")
    print("   Open two browser tabs to the same room URL to test a call.")
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)