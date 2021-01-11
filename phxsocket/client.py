import asyncio, websockets, json, logging, traceback
from threading import Event, Thread, Lock
from urllib.parse import urlencode
from .channel import Channel, ChannelEvents
from .message import Message


class SentMessage:
  def __init__(self, cb=None):
    self.cb = cb
    self.event = Event()
    self.message = None

  def respond(self, message):
    self.message = message
    if self.cb:
      self.cb(message)
    self.event.set()

  def wait_for_response(self):
    self.event.wait()
    return self.message


class ClientConnection(SentMessage):
  def wait(self):
    self.event.wait()
    if self.message:
      raise self.message
    return True

  def is_set(self):
    return self.event.is_set()


class Client:
  def __init__(self, url, params):
    self._url = url
    self.set_params(params)
    self._loop = None

    self._shutdown_evt = None

    self.channels = {}
    self.messages = {}
    self._ref_lock = Lock()
    self._ref = 0

    self.on_open = None
    self.on_message = None
    self.on_error = None
    self.on_close = None

    self.thread = None

    self._send_queue = None

  def set_params(self, params):
    qs_params = {"vsn": "1.0.0", **params}
    self.url = f"{self._url}?{urlencode(qs_params)}"

  async def _listen(self, websocket):
    async for msg in websocket:
      self._on_message(msg)

  async def _send(self, message):
    await self._send_queue.put(message)

  async def _broadcast(self, websocket, send_queue):
    while websocket.state == websockets.protocol.State.OPEN:
      message = await send_queue.get()
      if message:
        await websocket.send(message)
      send_queue.task_done()

  async def _run(self, loop, send_queue, connect_evt, shutdown_evt):
    async with websockets.connect(self.url) as websocket:
      connect_evt.respond(None)
      if self.on_open:
        self.on_open(self)
      loop.create_task(self._listen(websocket))
      loop.create_task(self._broadcast(websocket, send_queue))
      await shutdown_evt.wait()

  def run(self, connect_evt):
    self._loop = loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    self._send_queue = asyncio.Queue()
    self._shutdown_evt = asyncio.Event()

    tasks_pending = []

    try:
      loop.run_until_complete(
        self._run(loop, self._send_queue, connect_evt, self._shutdown_evt))
    except Exception as e:
      if not connect_evt.is_set():
        connect_evt.respond(e)
      elif self.on_error:
        self.on_error(self, e)
      else:
        logging.error("phxsocket: " + traceback.format_exc())
    finally:
      for task in asyncio.Task.all_tasks(loop):
        task.cancel()

      # notify self._broadcast
      loop.run_until_complete(self._send_queue.put(None))
      loop.close()
      self._loop = None

      Thread(target=self._on_close, args=[connect_evt], daemon=True).start()

  def _on_close(self, connect_evt):
    self.thread.join()
    if connect_evt.is_set() and self.on_close:
      self.on_close(self)

  def close(self):
    if not self._loop:
      logging.error("phxsocket: No loop found")
      return
    self._loop.call_soon_threadsafe(self._shutdown_evt.set)
    self.thread.join()

  def connect(self, blocking=True):
    if self._loop:
      logging.error("phxsocket: Trying to start another thread")
      return False

    connect_evt = ClientConnection()
    self.thread = Thread(target=self.run, args=[connect_evt], daemon=True)
    self.thread.start()

    if blocking:
      return connect_evt.wait()
    else:
      return connect_evt

  def _on_message(self, _message):
    message = Message.from_json(_message)

    if message.event == ChannelEvents.reply.value and message.ref in self.messages:
      self.messages[message.ref].respond(message.payload)
    else:
      channel = self.channels.get(message.topic)
      if channel:
        channel.receive(self, message)
      else:
        logging.info("phxsocket: Unknown message: {}".format(message))

    if message.ref in self.messages:
      del self.messages[message.ref]

    if self.on_message:
      Thread(target=self.on_message, args=[message], daemon=True).start()

  def push(self, topic, event, payload, cb=None, reply=False):
    if type(event) == ChannelEvents:
      event = event.value

    with self._ref_lock:
      ref = self._ref
      self._ref += 1

    message = json.dumps({
      "event": event,
      "topic": topic,
      "ref": ref,
      "payload": payload
    })

    sent_message = SentMessage(cb)

    if reply or cb:
      self.messages[ref] = sent_message

    asyncio.run_coroutine_threadsafe(self._send(message), loop=self._loop)

    if reply or cb:
      return sent_message

  def channel(self, topic, params={}):
    if topic not in self.channels:
      channel = Channel(self, topic, params)
      self.channels[topic] = channel

    return self.channels[topic]
