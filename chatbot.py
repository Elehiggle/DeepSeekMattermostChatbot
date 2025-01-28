import time
import traceback
import json
import threading
import re
import datetime
import concurrent.futures
from functools import lru_cache
import pymupdf
import pymupdf4llm
import httpx
from mattermostdriver_patched import Driver
from bs4 import BeautifulSoup
from youtube_transcript_api import YouTubeTranscriptApi
from yt_dlp import YoutubeDL
from openai import OpenAI, NOT_GIVEN
import tiktoken
from helpers import (
    yt_is_valid_url,
    yt_extract_video_id,
    split_message,
    is_valid_url,
    sanitize_username,
    timed_lru_cache,
)
from config import *  # pylint: disable=W0401 wildcard-import, unused-wildcard-import

logging.basicConfig(level=log_level_root)

logger = logging.getLogger(__name__)
logger.setLevel(log_level)

# Create a driver instance
driver = Driver(
    {
        "url": mattermost_url,
        "token": mattermost_token,
        "login_id": mattermost_username,
        "password": mattermost_password,
        "mfa_token": mattermost_mfa_token,
        "scheme": mattermost_scheme,
        "port": mattermost_port,
        "basepath": mattermost_basepath,
        "verify": MATTERMOST_CERT_VERIFY,
        "timeout": mattermost_timeout,
        # "websocket_kw_args": {"ping_interval": None},
    }
)

# Chatbot account username, automatically fetched
CHATBOT_USERNAME = ""
CHATBOT_USERNAME_AT = ""

# Create an AI client instance
ai_client = OpenAI(api_key=api_key, base_url=ai_api_baseurl)

# Used to count tokens, do not modify unless you know what you are doing
model_encoder = tiktoken.encoding_for_model("gpt-4o")

# Create a thread pool with a fixed number of worker threads
thread_pool = concurrent.futures.ThreadPoolExecutor(max_workers=5)


def get_system_instructions(initial_time):
    return system_prompt_unformatted.format(current_time=initial_time, CHATBOT_USERNAME=CHATBOT_USERNAME)


@lru_cache(maxsize=1000)
def get_username_from_user_id(user_id):
    try:
        user = driver.users.get_user(user_id)
        return sanitize_username(user["username"])
    except Exception as e:
        logger.error(f"Error retrieving username for user ID {user_id}: {str(e)} {traceback.format_exc()}")
        return f"Unknown_{user_id}"


def ensure_compliant_messages(messages):
    # For DeepSeek, messages must have alternating roles and (unconfirmed) the first message must always be a user role
    # Also, assistant messages must not be lists. https://github.com/cline/cline/issues/230#issuecomment-2576087368
    updated_messages = []
    acknowledged_content = "acknowledged"

    # Ensure first message is from user
    if not messages or messages[0]["role"] != "user":
        updated_messages.append({"role": "user", "content": acknowledged_content})

    # Process messages to ensure alternating roles and proper content format
    for message in messages:
        # Check for role alternation
        if updated_messages and message["role"] == updated_messages[-1]["role"]:
            # Insert intermediate dummy message with opposite role
            dummy_role = "assistant" if message["role"] == "user" else "user"
            updated_messages.append({
                "role": dummy_role,
                "content": acknowledged_content
            })

        # Process message content format for assistants
        processed_msg = message.copy()
        if processed_msg["role"] == "assistant":
            # Convert array content to string
            if isinstance(processed_msg["content"], list):
                text_parts = []
                for part in processed_msg["content"]:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text_parts.append(part.get("text", ""))
                processed_msg["content"] = "".join(text_parts)
            elif not isinstance(processed_msg["content"], str):
                processed_msg["content"] = str(processed_msg["content"])

        updated_messages.append(processed_msg)

    logger.debug(updated_messages)
    return updated_messages


def send_typing_indicator_loop(user_id, channel_id, parent_id, stop_event):
    """Send a "typing" indicator to show that work is in progress."""
    while not stop_event.is_set():
        try:
            # If full mode is active and we have a parent_id, also send an indicator to the main channel
            # We send this first because I prefer it and there is a slight lag for the second indicator
            if typing_indicator_mode_is_full and parent_id:
                options = {
                    "channel_id": channel_id,
                }

                driver.client.make_request("post", f"/users/{user_id}/typing", options=options)

            options = {"channel_id": channel_id, "parent_id": parent_id}  # id may be substituted with "me"

            driver.client.make_request("post", f"/users/{user_id}/typing", options=options)

            time.sleep(1)
        except Exception as e:
            logger.error(f"Error sending typing indicator: {str(e)} {traceback.format_exc()}")


def handle_typing_indicator(user_id, channel_id, parent_id):
    logger.debug("Starting typing indicator")
    stop_typing_event = threading.Event()
    typing_indicator_thread = threading.Thread(
        target=send_typing_indicator_loop,
        args=(user_id, channel_id, parent_id, stop_typing_event),
    )
    typing_indicator_thread.start()
    return stop_typing_event, typing_indicator_thread


def handle_text_generation(current_message, messages, channel_id, root_id, initial_time):
    start_time = time.time()

    system_instructions = get_system_instructions(initial_time)

    # Send the messages to the AI API
    response = ai_client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "system", "content": system_instructions}, *messages],
        timeout=timeout,
    )

    end_time = time.time()
    duration = end_time - start_time

    logger.debug(f"AI API response received after {duration:.2f} seconds")

    response_text = response.choices[0].message.content
    prompt_tokens = response.usage.prompt_tokens
    cached_prompt_tokens = response.usage.prompt_tokens_details.cached_tokens
    prompt_cache_hit_tokens = response.usage.prompt_cache_hit_tokens
    prompt_cache_miss_tokens = response.usage.prompt_cache_miss_tokens
    completion_tokens = response.usage.completion_tokens

    if response_text is None:
        raise Exception("Empty AI response, likely API error or mishandling")

    if response.choices[0].finish_reason == "content_filter":
        logger.debug("Response censored, finish reason: content_filter")
        response_text += "\n**Response censored, finish reason: content_filter**"

    # Split the response into multiple messages if necessary
    response_parts = split_message(response_text)

    # Send each part of the response as a separate message
    for part in response_parts:
        # Send the API response back to the Mattermost channel as a reply to the thread or as a new thread
        driver.posts.create_post({"channel_id": channel_id, "message": part, "root_id": root_id})

    prompt_tokens_cost = 0.55 / 1_000_000 * prompt_cache_miss_tokens + 0.14 / 1_000_000 * prompt_cache_hit_tokens
    completion_tokens_cost = 2.19 / 1_000_000 * completion_tokens
    tokens_cost_total = prompt_tokens_cost + completion_tokens_cost
    logger.debug(
        f"Text Token cost: ${tokens_cost_total:.4f} | Input ${prompt_tokens_cost:.4f} ({prompt_tokens}, cached: {cached_prompt_tokens}) + Output ${completion_tokens_cost:.4f} ({completion_tokens})"
    )


def handle_generation(current_message, messages, channel_id, root_id, initial_time):
    try:
        logger.info("Querying AI API")

        messages = ensure_compliant_messages(messages)

        handle_text_generation(current_message, messages, channel_id, root_id, initial_time)
    except json.decoder.JSONDecodeError as e:
        logger.error(f"Text generation error: {str(e)} {traceback.format_exc()}\nLikely API is down!")
        driver.posts.create_post(
            {"channel_id": channel_id, "message": f"Text generation error occurred: {str(e)}\nLikely AI API is down!", "root_id": root_id}
        )
    except Exception as e:
        logger.error(f"Text generation error: {str(e)} {traceback.format_exc()}")
        driver.posts.create_post(
            {"channel_id": channel_id, "message": f"Text generation error occurred: {str(e)}", "root_id": root_id}
        )


def process_message(event_data):
    post = json.loads(event_data["data"]["post"])
    if should_ignore_post(post):
        return

    current_message, channel_id, sender_name, root_id, post_id, channel_display_name = extract_post_data(
        post, event_data
    )

    stop_typing_event = None
    typing_indicator_thread = None
    chatbot_invoked = False

    try:
        messages = []

        # Chatbot is invoked if it was mentioned, the chatbot has already been invoked in the thread or its a DM
        chatbot_invoked = is_chatbot_invoked(post, post_id, root_id, channel_display_name)

        if chatbot_invoked:
            # Start the typing indicator
            stop_typing_event, typing_indicator_thread = handle_typing_indicator(
                driver.client.userid, channel_id, root_id
            )

            # Retrieve the thread context if there is any
            thread_messages = []

            if root_id:
                thread_messages = get_thread_posts(root_id, post_id)
                root_post = driver.posts.get_post(root_id)
                posted_at = root_post["create_at"]
            else:
                # If we don't have any thread, add our own message to the array
                thread_messages.append((post, sender_name, "user", current_message))
                posted_at = post["create_at"]

            current_time_utc = datetime.datetime.now(datetime.UTC)
            post_time_utc = datetime.datetime.fromtimestamp(posted_at / 1000.0, tz=datetime.UTC)
            initial_time = min(current_time_utc, post_time_utc).strftime("%Y-%m-%d %H:%M:%S.%f")[
                           :-3]  # Gets the UTC time of the root post

            for index, thread_message in enumerate(thread_messages):
                content = {}

                thread_post, thread_sender_name, thread_role, thread_message_text = thread_message

                image_messages = []

                links = re.findall(r"(https?://\S+)", thread_message_text, re.IGNORECASE)  # Allow http and https links
                content["website_data"] = []

                # We don't want to grab URL content from links the assistant sent
                # If keep URL content is disabled, we will skip the URL content code unless its the last message
                is_last_message = index == len(thread_messages) - 1
                if thread_role == "user" and keep_all_url_content or is_last_message:
                    for link in links:
                        website_data = {
                            "url": link,
                        }

                        try:
                            if not is_valid_url(link):
                                raise Exception("Local or invalid link")

                            website_data["url_content"], link_image_messages = request_link_content(link)
                            image_messages.extend(link_image_messages)
                        except Exception as e:
                            logger.error(
                                f"Error extracting content from link {link}: {str(e)} {traceback.format_exc()}"
                            )
                            website_data["error"] = (
                                f"fetching website caused an exception, warn the chatbot user: {str(e)}"
                            )
                        finally:
                            content["website_data"].append(website_data)

                files_text_content, files_image_messages = get_files_content(thread_post)
                image_messages.extend(files_image_messages)

                if files_text_content:
                    content["file_data"] = files_text_content
                if not content["website_data"]:
                    del content["website_data"]

                # We use str() and not JSON.dumps() to avoid the AI replying in (partially) escaped JSON format
                content = f"{str(content)}{thread_message_text}" if content else thread_message_text

                if image_messages:
                    image_messages.append({"type": "text", "text": content})
                    # We force a user role here, as this is an API requirement for images for GPT-4o
                    messages.append({"role": "user", "content": image_messages})  # "name": thread_sender_name in front
                else:
                    messages.append(construct_text_message(thread_sender_name, thread_role, content))

            # If the message is not part of a thread, reply to it to create a new thread
            handle_generation(current_message, messages, channel_id, post_id if not root_id else root_id, initial_time)
    except Exception as e:
        logger.error(f"Error processing message: {str(e)} {traceback.format_exc()}")
        if chatbot_invoked:
            driver.posts.create_post(
                {"channel_id": channel_id, "message": f"Process message error occurred: {str(e)}", "root_id": root_id}
            )
    finally:
        logger.debug("Clearing cache and stopping typing indicator")

        get_raw_thread_posts.cache_clear()  # We clear this cache as it won't be useful for the next message with the current implementation
        if stop_typing_event:
            stop_typing_event.set()
        if typing_indicator_thread:
            typing_indicator_thread.join()


def should_ignore_post(post):
    sender_id = post["user_id"]

    # Ignore own posts
    if sender_id == driver.client.userid:
        return True

    if sender_id in mattermost_ignore_sender_id:
        logger.debug("Ignoring post from an ignored sender ID")
        return True

    if post.get("props", {}).get("from_bot") == "true":
        logger.debug("Ignoring post from a bot")
        return True

    return False


def extract_post_data(post, event_data):
    # Remove the "@chatbot" mention from the message
    message = post["message"].replace(CHATBOT_USERNAME_AT, "").strip()
    channel_id = post["channel_id"]
    sender_name = sanitize_username(event_data["data"]["sender_name"])
    root_id = post["root_id"]
    post_id = post["id"]
    channel_display_name = event_data["data"]["channel_display_name"]
    return message, channel_id, sender_name, root_id, post_id, channel_display_name


def construct_text_message(name, role, message):
    return {
        # "name": name,
        "role": role,
        "content": [
            {
                "type": "text",
                "text": str(message),
            }
        ],
    }


# We pass post_id here so cache contains results for the most recent message
@lru_cache(maxsize=100)
def get_raw_thread_posts(root_id, _post_id):
    return driver.posts.get_thread(root_id)


def get_thread_posts(root_id, post_id):
    messages = []
    thread = get_raw_thread_posts(root_id, post_id)

    # Sort the thread posts based on their create_at timestamp as the "order" prop is not suitable for this
    sorted_posts = sorted(thread["posts"].values(), key=lambda x: x["create_at"])
    for thread_post in sorted_posts:
        thread_sender_name = get_username_from_user_id(thread_post["user_id"])
        thread_message = thread_post["message"].replace(CHATBOT_USERNAME_AT, "").strip()
        role = "assistant" if thread_post["user_id"] == driver.client.userid else "user"
        messages.append((thread_post, thread_sender_name, role, thread_message))
        if thread_post["id"] == post_id:
            break  # To prevent it answering a different newer post that we might have occurred during our processing

    return messages


def is_chatbot_invoked(post, post_id, root_id, channel_display_name):
    # We directly access the raw message here as we filter the mention earlier
    last_message = post["message"]
    if CHATBOT_USERNAME_AT in last_message:
        return True

    # It is a direct message
    if channel_display_name.startswith("@"):
        return True

    if root_id:
        thread = get_raw_thread_posts(root_id, post_id)

        # Check if the last post in the thread starts with a mention of ANY other bot than the chatbot
        # If so, ignore it, as it is likely a mention for another bot
        if thread:
            match = re.match(r"@(\w+)", last_message)

            if match:
                mentioned_username = match.group(1)

                try:
                    mentioned_user = driver.users.get_user_by_username(mentioned_username)
                    mentioned_user_id = mentioned_user["id"]

                    if mentioned_user_id != driver.client.userid and mentioned_user.get("is_bot", False):
                        logger.debug(
                            "Ignoring post and not checking further if we have been invoked as it is a mention for another bot"
                        )
                        return False
                except Exception as e:
                    logger.debug(f"Could not get user {mentioned_username}: {str(e)}")

        # Check if we have been mentioned in the past or if the chatbot had already replied
        for thread_post in thread["posts"].values():
            if thread_post["user_id"] == driver.client.userid:
                return True

            # Needed when you mention the chatbot and send a fast message afterward
            if CHATBOT_USERNAME_AT in thread_post["message"]:
                return True

    return False


@lru_cache(maxsize=100)
def get_file_content(file_details_json):
    file_details = json.loads(file_details_json)
    file_id = file_details["id"]
    file_size = file_details["size"]
    content_type = file_details["mime_type"].lower()
    image_messages = []

    if file_size / (1024**2) > max_response_size:
        raise Exception("File size exceeded the maximum limit for the chatbot")

    file = driver.files.get_file(file_id)
    if content_type.startswith("image/"):
        raise Exception("Images are not supported by this AI model")

    if "application/pdf" in content_type:
        return extract_pdf_content(file.content)

    # Return other files simply as string
    return str(file.content), image_messages


def extract_pdf_content(stream):
    pdf_text_content = ""
    image_messages = []

    with pymupdf.open(None, stream, "pdf") as pdf:
        pdf_text_content += pymupdf4llm.to_markdown(pdf, margins=0)

    return pdf_text_content, image_messages


def get_files_content(post):
    files_text_content_all = {}
    image_messages = []

    try:
        if post.get("metadata"):
            metadata = post["metadata"]
            if metadata.get("files"):
                metadata_files = metadata["files"]

                for file_details in metadata_files:
                    file_name = file_details["name"]
                    files_text_content_all[file_name] = {}

                    try:
                        files_text_content_all[file_name]["file_content"], file_image_messages = get_file_content(
                            json.dumps(file_details)
                        )  # JSON to make it cachable
                        image_messages.extend(file_image_messages)
                    except Exception as e:
                        logger.error(
                            f"Error extracting content from file {file_name}: {str(e)} {traceback.format_exc()}"
                        )
                        files_text_content_all[file_name][
                            "error"
                        ] = f"fetching file content caused an exception, warn the chatbot user: {str(e)}"
    except Exception as e:
        logger.error(f"Error get_files_content: {str(e)} {traceback.format_exc()}")

    return files_text_content_all, image_messages


async def message_handler(event):
    try:
        event_data = json.loads(event)
        logger.debug(f"Received event: {event_data}")
        if event_data.get("event") == "hello":
            logger.info("WebSocket connection established.")
        elif event_data.get("event") == "posted":
            # Submit the task to the thread pool. We do this because Mattermostdriver-async is outdated
            thread_pool.submit(process_message, event_data)
        else:
            # Handle other events
            pass
    except json.JSONDecodeError:
        logger.error(f"Failed to parse event as JSON: {event} {traceback.format_exc()}")
    except Exception as e:
        logger.error(f"Error message_handler: {str(e)} {traceback.format_exc()}")


def yt_find_preferred_transcript(video_id):
    transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)

    # Define the preferred order of transcript types and languages
    preferred_order = [
        ("manual", "en"),
        ("manual", None),
        ("generated", "en"),
        ("generated", None),
    ]

    # Convert the TranscriptList to a regular list
    transcripts = list(transcript_list)

    # Sort the transcripts based on the preferred order
    transcripts.sort(
        key=lambda t: (
            preferred_order.index((t.is_generated, t.language_code))
            if (t.is_generated, t.language_code) in preferred_order
            else len(preferred_order)
        )
    )

    # Return the first transcript in the sorted list
    return transcripts[0] if transcripts else None


def yt_get_transcript(url):
    video_id = yt_extract_video_id(url)
    preferred_transcript = yt_find_preferred_transcript(video_id)

    if preferred_transcript:
        transcript = preferred_transcript.fetch()
        return str(transcript)

    raise Exception("Error getting the YouTube transcript")


def yt_get_video_info(url):
    ydl_opts = {
        "quiet": True,
        # 'no_warnings': True,
    }

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

        title = info["title"]
        description = info["description"]
        uploader = info["uploader"]

        return title, description, uploader


def yt_get_content(link):
    transcript = yt_get_transcript(link)
    title, description, uploader = yt_get_video_info(link)
    return {
        "youtube_video_details": {
            "title": title,
            "description": description,
            "uploader": uploader,
            "transcript": transcript,
        }
    }


def request_flaresolverr(link):
    payload = {
        "cmd": "request.get",
        "url": link,
        "maxTimeout": 30000,
    }
    response = httpx.post(flaresolverr_endpoint, json=payload, timeout=30.0)
    response.raise_for_status()
    data = response.json()

    if data["status"] == "ok":
        # FlareSolverr always returns empty headers/200 status code, as per https://github.com/FlareSolverr/FlareSolverr/issues/1162
        content = data["solution"]["response"]
        return content

    raise Exception(f"FlareSolverr request failed: {data}")


def request_httpx(prev_response, content_type):
    content_chunks = []
    total_size = 0
    for chunk in prev_response.iter_bytes():
        content_chunks.append(chunk)
        total_size += len(chunk)
        if total_size > max_response_size:
            raise Exception("Website size exceeded the maximum limit for the chatbot")
    content = b"".join(content_chunks)
    if content_type.startswith("text/"):
        content = content.decode("utf-8", errors="surrogateescape")
    return content


def request_link_text_content(link, prev_response, content_type):
    raw_content = None
    try:
        # Note: FlareSolverr does not support returning content_type, so after redirections it could possibly be a different type
        if flaresolverr_endpoint:
            raw_content = request_flaresolverr(link)
        else:
            raise Exception("FlareSolverr endpoint not available")
    except Exception as e:
        logger.debug(f"Falling back to HTTPX. Reason: {str(e)}")

    if raw_content and "<title>New Tab</title>" in raw_content:
        logger.debug(
            "Website content is 'New Tab', retrying with HTTPX."
        )  # FlareSolverr issue I haven't figured out yet, happens with direct .CSV files for example
        raw_content = None

    if not raw_content:
        raw_content = request_httpx(prev_response, content_type)

    if content_type.startswith(("text/html", "application/xhtml+xml")):
        soup = BeautifulSoup(raw_content, "html.parser")
        website_content = soup.get_text(" | ", strip=True)

        tokens = len(model_encoder.encode(website_content))

        if tokens > 120000:
            logger.debug("Website text content too large, trying to extract article content only")
            article_texts = [article.get_text(" | ", strip=True) for article in soup.find_all("article")]
            website_content = " | ".join(article_texts)
    else:
        website_content = raw_content.strip()

    if not website_content:
        raise Exception("No text content found on website")

    return website_content


@timed_lru_cache(seconds=1800, maxsize=100)
def request_link_content(link):
    if yt_is_valid_url(link):
        return yt_get_content(link), []

    with httpx.Client() as client:
        # By doing the redirect itself, we might already allow a local request?
        with client.stream("GET", link, timeout=4, follow_redirects=True) as response:
            # Raise for bad status codes if we don't have a FlareSolverr endpoint, this can cause issues though if the requested content is not text
            if not flaresolverr_endpoint:
                response.raise_for_status()

            final_url = str(response.url)

            if not is_valid_url(final_url):
                logger.info(f"Skipping local/invalid URL {final_url} after redirection: {link}")
                raise Exception("Local/invalid URL is disallowed")

            content_type = response.headers.get("content-type", "").lower()
            if "image/" in content_type:
                raise Exception("Images are not supported by this AI model")

            if "application/pdf" in content_type:
                return request_link_pdf_content(response)

            return request_link_text_content(link, response, content_type), []


def request_link_pdf_content(prev_response):
    total_size = 0

    pdf_data = b""
    for chunk in prev_response.iter_bytes():
        pdf_data += chunk
        total_size += len(chunk)
        if total_size > max_response_size:
            raise Exception("PDF size from the website exceeded the maximum limit for the chatbot")

    return extract_pdf_content(pdf_data)


def main():
    try:
        global CHATBOT_USERNAME, CHATBOT_USERNAME_AT

        # Log in to the Mattermost server
        driver.login()

        CHATBOT_USERNAME = driver.client.username
        CHATBOT_USERNAME_AT = f"@{CHATBOT_USERNAME}"

        system_instructions = get_system_instructions(
            datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3])
        logger.debug(f"SYSTEM PROMPT: {system_instructions}")

        while True:
            try:
                # Initialize the WebSocket connection
                driver.init_websocket(message_handler)
            except Exception as e:
                logger.error(f"Error with WebSocket: {str(e)} {traceback.format_exc()}")
            time.sleep(2)

    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt, logout and exit")
        driver.logout()
    except Exception as e:
        logger.error(f"Error: {str(e)} {traceback.format_exc()}")


if __name__ == "__main__":
    main()
