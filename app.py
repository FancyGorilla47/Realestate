import os
import asyncio
import json
import logging
import base64
import traceback
import audioop  # Only needed for Twilio (Python 3.12 or lower)
from datetime import datetime
import pytz
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from dotenv import load_dotenv

# Twilio - for phone support
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream

# Import our real estate tools
from real_estate_tools import search_properties, get_property_details, REAL_ESTATE_TOOLS

# SDK Imports
from azure.ai.voicelive.aio import connect
from azure.core.credentials import AzureKeyCredential
from azure.ai.voicelive.models import (
    RequestSession,
    AzureStandardVoice,
    InputAudioFormat, 
    OutputAudioFormat,
    ServerVad,
    Modality,
    ServerEventType,
    ClientEventConversationItemCreate,
)

# Load environment variables
load_dotenv()

# Configuration
AZURE_ENDPOINT = os.getenv("AZURE_VOICELIVE_ENDPOINT")
AZURE_KEY = os.getenv("AZURE_VOICELIVE_API_KEY")
AZURE_MODEL = os.getenv("AZURE_VOICELIVE_MODEL", "gpt-4o-realtime")

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger("voice-agent")

# --- Step 1: Global Context & Background Worker ---

GLOBAL_CONTEXT = {
    "display_date": "",
    "iso_date": ""
}

async def update_doha_context():
    """Background task to keep Doha time context fresh."""
    while True:
        try:
            doha_tz = pytz.timezone('Asia/Qatar')
            now_doha = datetime.now(doha_tz)
            
            GLOBAL_CONTEXT['display_date'] = now_doha.strftime("%A, %B %d, %Y")
            GLOBAL_CONTEXT['iso_date'] = now_doha.strftime("%Y-%m-%d")
            
            log.info(f"Doha Context Updated: {GLOBAL_CONTEXT['iso_date']}")
        except Exception as e:
            log.error(f"Error updating context: {e}")
            
        await asyncio.sleep(300)  # Update every 5 minutes

@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- Step 3: Hot-Start the Context ---
    try:
        doha_tz = pytz.timezone('Asia/Qatar')
        now_doha = datetime.now(doha_tz)
        GLOBAL_CONTEXT['display_date'] = now_doha.strftime("%A, %B %d, %Y")
        GLOBAL_CONTEXT['iso_date'] = now_doha.strftime("%Y-%m-%d")
        log.info(f"Hot-Start Context: {GLOBAL_CONTEXT}")
    except Exception as e:
        log.error(f"Hot-Start Failed: {e}")
        now_utc = datetime.utcnow()
        GLOBAL_CONTEXT['display_date'] = now_utc.strftime("%A, %B %d, %Y")
        GLOBAL_CONTEXT['iso_date'] = now_utc.strftime("%Y-%m-%d")

    task = asyncio.create_task(update_doha_context())
    yield
    task.cancel()

app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def get():
    with open("static/index.html") as f:
        return HTMLResponse(f.read())

# --- TWILIO WEBHOOK ---
@app.post("/voice")
async def voice(request: Request):
    """Handle incoming Twilio calls by returning TwiML to connect to Media Stream."""
    response = VoiceResponse()
    twilio_connect = Connect()
    host = request.headers.get("host")
    stream_url = f'wss://{host}/media-stream'
    twilio_connect.stream(url=stream_url)
    response.append(twilio_connect)
    return Response(content=str(response), media_type="application/xml")

# --- HELPER: AUDIO TRANSCODING ---
def resample_mulaw_8k_to_pcm_24k(mulaw_chunk: bytes) -> bytes:
    """Twilio (Mulaw 8k) -> Azure (PCM16 24k)"""
    pcm_8k = audioop.ulaw2lin(mulaw_chunk, 2)
    pcm_24k, _ = audioop.ratecv(pcm_8k, 2, 1, 8000, 24000, None)
    return pcm_24k

def resample_pcm_24k_to_mulaw_8k(pcm_chunk_24k: bytes) -> bytes:
    """Azure (PCM16 24k) -> Twilio (Mulaw 8k)"""
    pcm_8k, _ = audioop.ratecv(pcm_chunk_24k, 2, 1, 24000, 8000, None)
    mulaw_code = audioop.lin2ulaw(pcm_8k, 2)
    return mulaw_code

# --- AGENT LOGIC (Browser & Twilio) ---
def get_system_instruction():
    try:
        with open("system_instruction.md", "r", encoding="utf-8") as f:
            base_instruction = f.read()
            
        # Inject dynamic context
        final_instruction = base_instruction.replace("{GLOBAL_CONTEXT['display_date']}", GLOBAL_CONTEXT['display_date'])
        final_instruction = final_instruction.replace("{GLOBAL_CONTEXT['iso_date']}", GLOBAL_CONTEXT['iso_date'])
        
        return final_instruction
    except Exception as e:
        log.error(f"Error loading system instruction: {e}")
        # Fallback to a minimal instruction if file read fails
        return f"You are Sara, a real estate consultant at Ezdan Real Estate. Today is {GLOBAL_CONTEXT['display_date']}."

async def run_browser_agent(client_ws: WebSocket):
    """Refactored logic for Browser (PCM16 24k direct)."""
    await client_ws.accept()
    log.info(f"Browser Client connected.")

    system_instruction = get_system_instruction()

    try:
        credential = AzureKeyCredential(AZURE_KEY)
        async with connect(endpoint=AZURE_ENDPOINT, model=AZURE_MODEL, credential=credential) as connection:
            log.info("Azure Connected (Browser Session).")

            session_config = RequestSession(
                modalities=[Modality.TEXT, Modality.AUDIO],
                voice=AzureStandardVoice(name="en-US-AvaMultilingualNeural", type="azure-standard", rate="0.98"), 
                instructions=system_instruction,
                tools=REAL_ESTATE_TOOLS,
                input_audio_format=InputAudioFormat.PCM16,
                output_audio_format=OutputAudioFormat.PCM16,
                turn_detection=ServerVad(threshold=0.6, silence_duration_ms=400)
            )
            await connection.session.update(session=session_config)
            
            # Trigger Greeting (Wait for Start)
            start_event = asyncio.Event()

            async def send_initial_greeting():
                await start_event.wait()
                await connection.response.create()

            # Browser -> Azure
            async def forward_to_azure():
                try:
                    while True:
                        data = await client_ws.receive_text()
                        message = json.loads(data)
                        if message['type'] == 'start':
                            start_event.set()
                        elif message['type'] == 'audio' and message['payload']:
                            await connection.input_audio_buffer.append(audio=message['payload'])
                        elif message['type'] == 'commit':
                            await connection.input_audio_buffer.commit()
                except WebSocketDisconnect:
                    log.info("WebSocket disconnected.")

            # Azure -> Browser
            async def forward_to_client():
                async for event in connection:
                    event_type = getattr(event, 'type', None)
                    
                    # Log user speech transcription
                    if event_type == ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED:
                        transcript = getattr(event, 'transcript', '')
                        if transcript:
                            log.info(f"🎤 USER: {transcript}")
                    
                    # Log LLM text response
                    elif event_type == ServerEventType.RESPONSE_TEXT_DONE:
                        text = getattr(event, 'text', '')
                        if text:
                            log.info(f"🤖 SARA: {text}")
                    
                    elif event_type == ServerEventType.RESPONSE_AUDIO_DELTA:
                        delta = getattr(event, 'delta', b'')
                        if delta:
                            encoded = base64.b64encode(delta).decode('utf-8')
                            await client_ws.send_json({"type": "audio", "payload": encoded})
                    elif event_type == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED:
                        log.info("🎙️ User started speaking...")
                        await client_ws.send_json({"type": "clear_audio"})
                        await connection.response.cancel()
                    elif event_type == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STOPPED:
                        log.info("🎙️ User stopped speaking")
                    elif event_type == ServerEventType.RESPONSE_FUNCTION_CALL_ARGUMENTS_DONE:
                        call_id = getattr(event, 'call_id', None)
                        name = getattr(event, 'name', None)
                        arguments = getattr(event, 'arguments', "{}")
                        if call_id:
                            asyncio.create_task(handle_tool_call(connection, call_id, name, arguments))

            await asyncio.gather(forward_to_azure(), forward_to_client(), send_initial_greeting())

    except Exception as e:
        log.error(f"Browser Session Error: {e}")
        await client_ws.close()

async def run_twilio_agent(client_ws: WebSocket):
    """Logic for Twilio (Mulaw 8k <-> Transcoding <-> PCM16 24k)."""
    await client_ws.accept()
    log.info(f"Twilio Client connected.")
    
    stream_sid = None
    stream_start_event = asyncio.Event()
    start_time = 0
    system_instruction = get_system_instruction()

    try:
        credential = AzureKeyCredential(AZURE_KEY)
        async with connect(endpoint=AZURE_ENDPOINT, model=AZURE_MODEL, credential=credential) as connection:
            log.info("Azure Connected (Twilio Session).")

            session_config = RequestSession(
                modalities=[Modality.TEXT, Modality.AUDIO],
                voice=AzureStandardVoice(name="en-US-AvaMultilingualNeural", type="azure-standard", rate="0.9"), 
                instructions=system_instruction,
                tools=REAL_ESTATE_TOOLS,
                input_audio_format=InputAudioFormat.PCM16,
                output_audio_format=OutputAudioFormat.PCM16,
                turn_detection=ServerVad(threshold=0.9, silence_duration_ms=1000, prefix_padding_ms=300)
            )
            await connection.session.update(session=session_config)
            
            # Trigger Greeting (Wait for Stream ID)
            async def send_initial_greeting():
                await stream_start_event.wait()
                await asyncio.sleep(0.05) # Small buffer
                await connection.response.create()

            # Twilio -> Azure
            async def forward_to_azure():
                nonlocal stream_sid, start_time
                try:
                    while True:
                        data = await client_ws.receive_text()
                        packet = json.loads(data)
                        
                        if packet['event'] == 'start':
                            stream_sid = packet['start']['streamSid']
                            log.info(f"Twilio Stream Started: {stream_sid}")
                            stream_start_event.set()
                            start_time = asyncio.get_running_loop().time()
                        
                        elif packet['event'] == 'media':
                            # Audio Gate: Ignore first 1.2s to prevent barge-in noise
                            if asyncio.get_running_loop().time() - start_time < 1.2:
                                continue

                            # 1. Decode Base64 (Mulaw)
                            mulaw_payload = base64.b64decode(packet['media']['payload'])
                            # 2. Transcode (Mulaw 8k -> PCM 24k)
                            pcm_24k = resample_mulaw_8k_to_pcm_24k(mulaw_payload)
                            # 3. Encode Base64 (PCM)
                            pcm_b64 = base64.b64encode(pcm_24k).decode('utf-8')
                            
                            # Send to Azure
                            await connection.input_audio_buffer.append(audio=pcm_b64)
                        
                        elif packet['event'] == 'stop':
                            log.info("Twilio Stream Stopped.")
                            break
                            
                except WebSocketDisconnect:
                    log.info("Twilio WebSocket disconnected.")

            # Azure -> Twilio
            async def forward_to_client():
                async for event in connection:
                    event_type = getattr(event, 'type', None)
                    
                    if event_type == ServerEventType.RESPONSE_AUDIO_DELTA:
                        delta = getattr(event, 'delta', b'')
                        if delta and stream_sid:
                            # 1. Transcode (PCM 24k -> Mulaw 8k)
                            mulaw_chunk = resample_pcm_24k_to_mulaw_8k(delta)
                            # 2. Encode Base64
                            payload_b64 = base64.b64encode(mulaw_chunk).decode('utf-8')
                            
                            # Send 'media' event to Twilio
                            await client_ws.send_json({
                                "event": "media",
                                "streamSid": stream_sid,
                                "media": {"payload": payload_b64}
                            })

                    elif event_type == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED:
                        if stream_sid:
                            await client_ws.send_json({
                                "event": "clear",
                                "streamSid": stream_sid
                            })
                        await connection.response.cancel()

                    elif event_type == ServerEventType.RESPONSE_FUNCTION_CALL_ARGUMENTS_DONE:
                        call_id = getattr(event, 'call_id', None)
                        name = getattr(event, 'name', None)
                        arguments = getattr(event, 'arguments', "{}")
                        if call_id:
                            asyncio.create_task(handle_tool_call(connection, call_id, name, arguments))

            await asyncio.gather(forward_to_azure(), forward_to_client(), send_initial_greeting())

    except Exception as e:
        log.error(f"Twilio Session Error: {e}")
        await client_ws.close()

# --- SHARED TOOL HANDLER ---
async def handle_tool_call(connection, call_id, name, arguments):
    log.info(f"="*50)
    log.info(f"🔧 TOOL CALL: {name}")
    log.info(f"📥 ARGUMENTS: {arguments}")
    
    try:
        args = json.loads(arguments)
        result_json = "{}"

        if name == "search_properties":
            log.info(f"   Query: {args.get('query', 'N/A')}")
            log.info(f"   Location: {args.get('location', 'Any')}")
            log.info(f"   Type: {args.get('property_type', 'Any')}")
            log.info(f"   Bedrooms: {args.get('bedrooms', 'Any')}")
            log.info(f"   Price: {args.get('min_price', 'Min')}-{args.get('max_price', 'Max')} QAR")
            
            result_json = await search_properties(
                query=args.get('query', ''),
                property_type=args.get('property_type', ''),
                location=args.get('location', ''),
                min_price=args.get('min_price'),
                max_price=args.get('max_price'),
                bedrooms=args.get('bedrooms')
            )
            
            # Log result summary
            result_data = json.loads(result_json)
            found = result_data.get('found', 0)
            log.info(f"📊 SEARCH RESULTS: {found} properties found")
            if found > 0 and 'properties' in result_data:
                for i, prop in enumerate(result_data['properties'][:3]):
                    log.info(f"   [{i+1}] {prop.get('title', 'N/A')[:40]}... | {prop.get('location')} | {prop.get('price_qar')} QAR")
                if found > 3:
                    log.info(f"   ... and {found - 3} more")
        
        elif name == "get_property_details":
            reference_number = args.get('reference_number', '')
            log.info(f"   Reference: {reference_number}")
            result_json = await get_property_details(reference_number)
            
            result_data = json.loads(result_json)
            if result_data.get('found'):
                prop = result_data.get('property', {})
                log.info(f"📊 PROPERTY FOUND: {prop.get('title', 'N/A')}")
            else:
                log.info(f"📊 PROPERTY NOT FOUND")
        
        else:
            result_json = json.dumps({"error": "Unknown tool called."})
            log.warning(f"⚠️ Unknown tool: {name}")
        
        log.info(f"="*50)
        
        item = ClientEventConversationItemCreate(
            item={
                "type": "function_call_output",
                "call_id": call_id,
                "output": result_json
            }
        )
        await connection.send(item)
        await connection.response.create()
        
    except Exception as tool_err:
        log.error(f"❌ Tool execution failed: {tool_err}")
        log.error(f"="*50)

# --- ROUTES ---

@app.websocket("/ws")
async def websocket_browser(websocket: WebSocket):
    await run_browser_agent(websocket)

# --- TWILIO WEBSOCKET ---
@app.websocket("/media-stream")
async def websocket_twilio(websocket: WebSocket):
    await run_twilio_agent(websocket)
