# Setting up with Claude Code

This prompt has Claude Code install and connect everything reel needs, on Windows or macOS: SABnzbd to download, reel to search and pick, Prowlarr, Sonarr and Radarr to grab new movies and episodes automatically, and Jellyfin if you want to watch on a TV.

Have these ready before you start:

- your usenet provider's server address, port, username, password and how many connections your plan allows (it's on the provider's website, usually under "account" or "setup")
- your NZBGeek API key (sign in at nzbgeek.info, open your profile, copy the API key)

It takes about half an hour, most of it waiting for installs. Claude Code asks before installing anything and asks for each password when it needs it.

To use it: install Claude Code (https://claude.com/claude-code), open Windows Terminal (Windows) or Terminal (Mac), type `claude`, and paste everything in the box below.

```
Set up a home usenet download system on this computer for me. I am not technical, so before each step tell me in one or two plain sentences what you are about to do, and ask me before you install anything.

What I want at the end:
- SABnzbd downloading from my usenet provider
- reel (https://github.com/voidharbor/reel), a terminal app for searching NZBGeek and picking releases, working when I type `reel`
- Prowlarr holding my NZBGeek indexer and sharing it with Sonarr and Radarr
- Radarr grabbing movies and Sonarr grabbing TV episodes automatically, both downloading through SABnzbd
- if I say yes when you ask: Jellyfin, so I can watch my library on the TV

How to work:
- First find out whether this is Windows or macOS. On Windows use winget to install things; on macOS use Homebrew (ask me before installing Homebrew if it's missing). Use `winget search` or `brew search` to find each app's official package instead of guessing the name.
- Ask me for each secret only when you need it: the usenet server details, my username and password, my NZBGeek API key. Never repeat a password back to me and never save one anywhere except inside the app that needs it.
- Do not open or forward any ports on my router. Turn on the login screen (authentication) in Sonarr, Radarr and Prowlarr, and tell me the username and password you set, once, so I can write them down.
- Make each app start by itself when the computer starts.
- Check that each step worked before you start the next one. If something fails, explain it plainly and try to fix it before moving on.

Steps:
1. Ask me where my library should live. Suggest a Media folder in my home folder with Movies and TV inside it (~/Media/Movies and ~/Media/TV). Create the folders.
2. Python: make sure Python 3.9 or newer is installed (on Windows, Python 3.12 from winget is a good choice).
3. SABnzbd: install it and open its web page (http://localhost:8080). Finish its setup wizard with my usenet server details (use SSL, usually port 563, and the number of connections my plan allows). Use the wizard's Test Server button to check them. Create three categories under Config, Categories: "movies" and "tv" (for Radarr and Sonarr) and "reel" (for reel). Get its API key from Config, General, Security.
4. reel: install it with pip from https://github.com/voidharbor/reel/archive/refs/heads/main.zip (on Windows: `py -m pip install --user <that url>`; on macOS use `python3 -m pip install --user <that url>`, or pipx if pip refuses with "externally-managed-environment"). Then write its settings file directly, because its own setup screen is interactive. The file is %APPDATA%\reel\config.json on Windows and ~/.config/reel/config.json on macOS:
   {
     "indexer": {"name": "NZBGeek", "url": "https://api.nzbgeek.info/api", "api_key": "<my NZBGeek API key>"},
     "sabnzbd": {"url": "http://127.0.0.1:8080", "api_key": "<SABnzbd API key>", "category": "reel"},
     "library": {"movies": "<movies folder>", "tv": "<tv folder>"}
   }
   Run `reel --check` (or `py -m reel --check` / `python3 -m reel --check`) and fix anything that says FAIL. Then make sure plain `reel` works in a brand new terminal window; if it doesn't, add the folder pip put it in to my PATH, or set up pipx with `pipx ensurepath`.
5. Prowlarr: install it, open http://localhost:9696, and add NZBGeek as an indexer with my API key. Test it.
6. Radarr: install it, open http://localhost:7878. Add a root folder pointing at my movies folder. Add SABnzbd as the download client (host localhost, port 8080, its API key, category "movies") and test it. Set the default quality profile to HD-1080p.
7. Sonarr: install it, open http://localhost:8989. Add a root folder pointing at my TV folder. Add SABnzbd as the download client (category "tv") and test it. Set the default quality profile to HD-1080p.
8. Back in Prowlarr, under Settings, Apps, add Radarr and Sonarr using their API keys (each one's API key is under Settings, General) so they get the NZBGeek indexer. Check that the indexer shows up in both.
9. Ask me if I want Jellyfin. If yes, install it, open http://localhost:8096, create my user, and add two libraries: Movies (my movies folder) and Shows (my TV folder).
10. Finish: run `reel --check` one more time and show me the result. Then write a short note to a text file on my Desktop called "Media setup notes.txt" with: the web address of each app, where my movies and TV go, how to start reel (type `reel` in Windows Terminal or Terminal), and the reel keys (type a title, Enter to search, arrows and Enter to pick, Esc to go back, Esc on the search screen to quit). Do not put any passwords or API keys in that note.
```

When it's done, open a new terminal window and type `reel`.
