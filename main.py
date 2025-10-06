import discord
from discord import app_commands
from discord.ext import tasks
import os
import asyncio
import aiohttp
import time
from typing import Optional

# --- Configuration & Setup ---

# Discord Application IDs provided for reference
APPLICATION_ID = '1424857080992497666'
PUBLIC_KEY = '995054885bebfc921204ac2eccaba20ee1ec07a598061f1a8995f05b7bc098f0'

# Load environment variables for security and deployment flexibility
DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

# Check for required tokens
if not DISCORD_BOT_TOKEN:
    print("FATAL ERROR: DISCORD_BOT_TOKEN environment variable not set. Exiting.")
    exit()

if not GEMINI_API_KEY:
    print("WARNING: GEMINI_API_KEY environment variable not set. The /automatic command will not function.")

# --- Gemini API Service ---

class GeminiService:
    """Handles asynchronous calls to the Gemini API for message generation."""
    def __init__(self, api_key: str):
        self.api_key = api_key
        # Using gemini-2.5-flash-preview-05-20 as the specified model
        self.api_url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-2.5-flash-preview-05-20:generateContent?key="
        )

    async def generate_content(self, prompt: str) -> str:
        """Calls the Gemini API to get an AI-generated response."""
        if not self.api_key:
            return "Error: Gemini API key is missing. Cannot generate content."

        url = f"{self.api_url}{self.api_key}"
        
        # System instruction to guide the bot's persona and output format
        system_instruction = (
            "You are a friendly, concise, and helpful Discord channel announcer. "
            "Respond to the user's prompt by generating a short, engaging, and "
            "single-paragraph message for a Discord chat."
        )

        payload = {
            "contents": [{"parts": [{"text: prompt}]}],
            "systemInstruction": {"parts": [{"text": system_instruction}]},
        }

        # Use aiohttp for asynchronous HTTP requests
        async with aiohttp.ClientSession() as session:
            try:
                # Implementing basic exponential backoff for retries
                for i in range(3): # Try up to 3 times
                    async with session.post(url, json=payload, timeout=20) as response:
                        if response.status == 200:
                            result = await response.json()
                            text = result.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', 'Failed to parse AI response.')
                            return text
                        elif response.status == 429:
                            # Too Many Requests - apply backoff
                            delay = 2 ** i
                            print(f"API Rate Limit hit, retrying in {delay}s...")
                            await asyncio.sleep(delay)
                        else:
                            error_text = await response.text()
                            return f"AI API Error ({response.status}): {error_text}"
                return "AI API failed after multiple retries due to rate limiting or server issues."
            except aiohttp.ClientError as e:
                return f"Network or API communication error: {e}"
            except Exception as e:
                return f"An unexpected error occurred during AI generation: {e}"


# --- Discord Bot Implementation ---

class ScheduledMessageBot(discord.Client):
    def __init__(self, *, intents: discord.Intents):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

        # Scheduling configuration (stores the state of the scheduled message)
        self.scheduled_task = None
        self.interval_seconds: Optional[int] = None
        self.channel_id: Optional[int] = None
        self.message_content: Optional[str] = None
        self.ai_prompt: Optional[str] = None
        self.mode: Optional[str] = None # 'manual' or 'automatic'
        
        # Anti-Stacking/Activity Tracking variables
        self.last_bot_send_time: float = 0.0 # Unix timestamp of when the bot last sent the scheduled message
        self.last_channel_activity_time: float = time.time() # Unix timestamp of the last message sent by anyone in the channel

        # Initialize AI service
        self.gemini_service = GeminiService(GEMINI_API_KEY)


    async def on_ready(self):
        """Called when the bot successfully connects to Discord."""
        await self.tree.sync() # Sync commands globally/per guild
        print(f'Logged in as {self.user} (ID: {self.user.id})')
        print('Ready to receive commands.')

        # Start the background loop when the bot is ready
        if self.scheduled_task is None or not self.scheduled_task.is_running():
            # The loop is started once and runs forever, checking the schedule and state every 60s.
            self.scheduled_task = self.send_scheduled_message.start()


    async def on_message(self, message: discord.Message):
        """Updates the last channel activity time."""
        # Ignore messages sent by the bot itself or system messages
        if message.author == self.user or message.author.bot:
            return

        # We only care about activity in the *scheduled* channel to track silence
        if self.channel_id is not None and message.channel.id == self.channel_id:
            self.last_channel_activity_time = time.time()
            
    
    @tasks.loop(seconds=60) # Check every 60 seconds
    async def send_scheduled_message(self):
        """The core background task that implements the scheduling and anti-stacking logic."""
        if self.channel_id is None or self.interval_seconds is None:
            return # Task is running but no schedule is set

        # 1. Check if the required time interval has passed since the last successful send
        time_since_last_send = time.time() - self.last_bot_send_time
        if time_since_last_send < self.interval_seconds:
            return

        # 2. Implement the Anti-Stacking/Activity Check
        # If the last channel activity was at or before the bot's last send time, 
        # it means the channel has been quiet since the last scheduled message. Skip sending.
        if self.last_channel_activity_time <= self.last_bot_send_time:
            # print("Channel is quiet. Skipping send to prevent spam.")
            return

        # 3. Time has passed AND channel has been active. Proceed to send.
        
        target_channel = self.get_channel(self.channel_id)
        if not target_channel:
            print(f"Error: Scheduled channel with ID {self.channel_id} not found.")
            return

        message_to_send = self.message_content

        # If in automatic mode, generate content first
        if self.mode == 'automatic' and self.ai_prompt:
            if not GEMINI_API_KEY:
                print("Skipping automatic message generation: GEMINI_API_KEY is missing.")
                message_to_send = "Automatic message generation failed: API Key missing."
            else:
                # AI generation is the new message to send
                message_to_send = await self.gemini_service.generate_content(self.ai_prompt)
        
        # 4. Send the message
        try:
            await target_channel.send(message_to_send)
            # Update the bot's last send time immediately after successful send
            self.last_bot_send_time = time.time()
            print(f"Scheduled message ({self.mode} mode) sent successfully.")
        except discord.Forbidden:
            print(f"Error: Bot does not have permission to send messages in channel {target_channel.name}.")
        except Exception as e:
            print(f"An error occurred while sending the message: {e}")

    # --- Slash Commands ---

    @app_commands.command(name="manual", description="Schedule a single message to be sent repeatedly.")
    @app_commands.describe(
        message="The message to send repeatedly.",
        interval_hours="The interval in hours between messages (must be >= 1).",
    )
    async def manual(self, interaction: discord.Interaction, message: str, interval_hours: int):
        await interaction.response.defer(ephemeral=True, thinking=True)

        if interval_hours < 1:
            await interaction.followup.send("The interval must be 1 hour or more.", ephemeral=True)
            return

        # Convert hours to seconds for the internal timer
        interval_seconds = interval_hours * 3600

        # Stop existing task if running (though the loop runs, canceling ensures a state reset)
        if self.scheduled_task is not None and self.scheduled_task.is_running():
            self.scheduled_task.cancel()
        
        # Set new configuration
        self.mode = 'manual'
        self.message_content = message
        self.ai_prompt = None # Clear AI prompt
        self.interval_seconds = interval_seconds
        self.channel_id = interaction.channel_id
        self.last_bot_send_time = 0.0 # Reset timer to allow for immediate send check
        self.last_channel_activity_time = time.time() # Assume channel is active since command was just sent

        # Restart the task loop (it handles starting/restarting itself in on_ready, but we re-start it here too)
        self.scheduled_task = self.send_scheduled_message.start()

        await interaction.followup.send(
            f"**Manual Schedule Set!**\n"
            f"The following message will be sent in <#{self.channel_id}> "
            f"every **{interval_hours} hour(s)**, but only if there has been "
            f"activity in the channel since the last scheduled message was sent:\n"
            f"
      
