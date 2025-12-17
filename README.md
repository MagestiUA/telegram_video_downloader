# Telegram Video Downloader Bot

A Telegram bot that automatically downloads videos from Telegram chats and organizes them with AI-powered metadata extraction.

## Features

- **Smart Download Queue**: Downloads are processed sequentially to prevent bandwidth saturation
- **Multi-threaded Download**: Utilizes parallel chunk downloading for faster transfers
- **AI-Powered Metadata Extraction**: Uses Google Gemini to extract anime titles, seasons, and episode numbers from filenames and captions
- **Title Mapping**: Maintains a persistent database of recognized anime titles for consistent naming
- **Organized Storage**: Automatically creates directory structures based on canonical anime names
- **User Access Control**: Restricts bot usage to authorized Telegram users
- **Interactive Title Confirmation**: Prompts users to confirm unknown anime titles before saving

## Prerequisites

- Python 3.8+
- Telegram API credentials (API ID, API Hash, Bot Token)
- Google Gemini API key (for AI metadata extraction)
- Docker (optional, for containerized deployment)

## Installation

### 1. Clone the repository
```bash
git clone https://github.com/yourusername/tg_video_downloader.git
cd tg_video_downloader
```

### 2. Install dependencies
```bash
pip install -r requirements.txt
```

### 3. Configure environment variables
Copy the `.env.template` file to `.env` and fill in your credentials:

```bash
cp .env.template .env
```

Edit `.env` with your values:
- `API_ID` - Telegram API ID from https://my.telegram.org
- `API_HASH` - Telegram API Hash
- `BOT_TOKEN` - Bot token from @BotFather
- `GEMINI_API_KEY` - Google Gemini API key from Google AI Studio
- `DOWNLOAD_PATH` - Absolute path where videos will be saved
- `ALLOWED_USERS` - Comma-separated list of Telegram user IDs allowed to use the bot

### 4. Run the bot
```bash
python main.py
```

## Docker Deployment

Build and run using Docker Compose:

```bash
docker-compose up -d
```

## Project Structure

```
tg_video_downloader/
├── analyzer/           # AI metadata extraction and title mapping
│   ├── ai_cleaner.py  # Google Gemini integration
│   └── mapper.py      # Persistent title mapping storage
├── config/            # Configuration management
├── core/              # Core functionality
│   ├── downloader.py  # Multi-threaded video downloader
│   ├── queue_manager.py # Download queue management
│   └── renamer.py     # File naming and organization
├── main.py            # Bot entry point
└── requirements.txt   # Python dependencies
```

## Usage

1. Start a conversation with your bot on Telegram
2. Send `/start` to get your User ID
3. Add your User ID to `ALLOWED_USERS` in `.env`
4. Send or forward video files to the bot
5. The bot will:
   - Analyze the filename/caption with AI
   - Check if the anime title is known
   - Ask for confirmation if unknown
   - Add the download to the queue
   - Process downloads sequentially
   - Save files with organized naming: `Anime Title - S01E05.ext`

## Features in Detail

### Sequential Download Queue
The bot implements a queue system to ensure downloads are processed one at a time, preventing bandwidth throttling from Telegram's API. When multiple videos are sent, they are queued and processed sequentially.

### AI Metadata Extraction
Uses Google Gemini to intelligently parse filenames and captions to extract:
- Anime title (preferably in Romaji)
- Season number
- Episode number

### Title Mapping
Unknown titles prompt the user for confirmation. Once confirmed, titles are saved to `mappings.json` for future automatic recognition.

## License

MIT License - see LICENSE file for details

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.
