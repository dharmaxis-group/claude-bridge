# Features

<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body>

<p style="font-size:16px;color:#8888a0;margin-bottom:32px;">
Everything Claude Bridge can do — organized by category. All features work from your Telegram chat.
</p>

<h2>Core Communication</h2>

<p><strong>Text Messages</strong> — Send any message and Claude Code processes it as a prompt. Full multi-turn conversation with session persistence.</p>

<p><strong>Image Analysis</strong> — Send photos (with or without captions). Claude sees the image and responds — screenshot debugging, UI review, document scanning.</p>

<p><strong>Voice Conversation</strong> — Send a voice message, it's transcribed locally via Whisper, Claude responds with both text and spoken audio.</p>

<p><strong>Document Handling</strong> — Send PDF, code files, or logs directly. Claude reads and analyzes the content without you needing to describe it.</p>

<p><strong>Streaming Progress</strong> — See what Claude is doing in real-time: which files it's reading, what commands it's running, what it's editing. No more staring at a blank screen.</p>

<h2>Voice System</h2>

<p><strong><code>/voice</code> — Voice Settings</strong> — Toggle voice replies on/off, switch between TTS engines, browse and preview voices before selecting.</p>

<p><strong>Two TTS Engines</strong></p>
<ul>
  <li><strong>edge-tts</strong> — Free, 8 Chinese voices including Liaoning and Shaanxi dialects</li>
  <li><strong>ElevenLabs</strong> — Cloud TTS with 21 premium voices, natural intonation</li>
</ul>

<p><strong><code>/el</code> — ElevenLabs Dashboard</strong> — Check remaining credits, monthly usage, voice slots, and next billing date — all from Telegram.</p>

<h2>Project Management</h2>

<p><strong><code>/p</code> — Switch Projects</strong> — Tap to switch between registered projects. Each project has its own CLAUDE.md, session state, and cost tracking. InlineKeyboard for quick selection.</p>

<p><strong><code>/p add</code> / <code>/p rm</code></strong> — Register or remove projects on the fly.</p>

<p><strong><code>/model</code> — Model Selection</strong> — Switch between Opus (deep reasoning) and Sonnet (fast responses).</p>

<p><strong><code>/effort</code> — Thinking Depth</strong> — Set Low / Medium / High to control how deeply Claude thinks before responding.</p>

<p><strong><code>/think</code> — Deep Mode</strong> — One tap to switch to Opus + High effort for complex tasks.</p>

<h2>Task Orchestration &amp; Automation</h2>

<p><strong><code>/task</code> — Two-Phase Execution</strong> — For operations that modify your system: Phase 1 analyzes in readonly mode and presents a plan. You review and confirm. Phase 2 executes with full tool access. Safe by design.</p>

<p><strong><code>/cron</code> — Scheduled Tasks</strong> — Register prompts that run on a schedule. Add, list, pause, resume, or remove recurring jobs — health checks, reports, deployments, anything.</p>

<h2>Cost Control</h2>

<p><strong><code>/budget</code> — Daily Budget</strong> — Set a spending cap. Toggle on/off, adjust the amount. Persisted in SQLite, takes effect immediately without restart.</p>

<p><strong><code>/cost</code> — Cost Reports</strong> — View spending: today, last 7 days, broken down by project. Know exactly where your budget goes.</p>

<p><strong>Smart Model Routing</strong> — Short, simple queries (under 80 characters, non-complex) automatically use Sonnet + Low effort. Complex tasks stay on Opus. Saves cost and responds faster — no manual switching needed.</p>

<h2>Security</h2>

<p><strong>User Whitelist</strong> — Only Telegram user IDs in <code>allowFrom</code> can interact with the bot. Everyone else is silently ignored.</p>

<p><strong>Keychain Integration</strong> — Bot token stored in macOS Keychain, not plaintext config. Referenced via <code>!security</code> prefix in config.json.</p>

<p><strong>Sensitive Data Masking</strong> — Passwords, tokens, and API keys in your messages are automatically masked in Claude's responses.</p>

<p><strong>Auto-Delete Sensitive Messages</strong> — Messages containing passwords or tokens are automatically deleted from the chat after processing.</p>

<h2>Operations</h2>

<p><strong><code>/restart</code> — Remote Restart</strong> — Restart the Claude Bridge service from Telegram. LaunchAgent automatically respawns the process.</p>

<p><strong><code>/new</code> — New Session</strong> — Reset the current conversation. Start fresh without restarting the service.</p>

<p><strong><code>/status</code> — Status Dashboard</strong> — Current project, model, effort level, session info, and all available actions in one view.</p>

<p><strong>LaunchAgent</strong> — Runs as a macOS background service. Starts on boot, restarts on crash. Zero maintenance once configured.</p>

<p><strong>Per-User FIFO Lock</strong> — Each user's requests are queued in order. A global semaphore limits concurrent workers to prevent resource exhaustion.</p>

<h2>Command Reference</h2>

<table style="width:100%;border-collapse:collapse;margin:16px 0;">
  <thead>
    <tr style="border-bottom:1px solid rgba(255,255,255,0.1);">
      <th style="text-align:left;padding:8px 12px;color:#8888a0;font-size:13px;">Command</th>
      <th style="text-align:left;padding:8px 12px;color:#8888a0;font-size:13px;">Description</th>
    </tr>
  </thead>
  <tbody>
    <tr style="border-bottom:1px solid rgba(255,255,255,0.04);">
      <td style="padding:8px 12px;"><code>/start</code> <code>/help</code></td>
      <td style="padding:8px 12px;color:#8888a0;">Help and quick actions</td>
    </tr>
    <tr style="border-bottom:1px solid rgba(255,255,255,0.04);">
      <td style="padding:8px 12px;"><code>/status</code></td>
      <td style="padding:8px 12px;color:#8888a0;">Current status + all action buttons</td>
    </tr>
    <tr style="border-bottom:1px solid rgba(255,255,255,0.04);">
      <td style="padding:8px 12px;"><code>/p</code></td>
      <td style="padding:8px 12px;color:#8888a0;">Switch project</td>
    </tr>
    <tr style="border-bottom:1px solid rgba(255,255,255,0.04);">
      <td style="padding:8px 12px;"><code>/model</code></td>
      <td style="padding:8px 12px;color:#8888a0;">Switch model (Opus / Sonnet)</td>
    </tr>
    <tr style="border-bottom:1px solid rgba(255,255,255,0.04);">
      <td style="padding:8px 12px;"><code>/effort</code></td>
      <td style="padding:8px 12px;color:#8888a0;">Thinking depth (Low / Medium / High)</td>
    </tr>
    <tr style="border-bottom:1px solid rgba(255,255,255,0.04);">
      <td style="padding:8px 12px;"><code>/think</code></td>
      <td style="padding:8px 12px;color:#8888a0;">One-key Opus + High effort</td>
    </tr>
    <tr style="border-bottom:1px solid rgba(255,255,255,0.04);">
      <td style="padding:8px 12px;"><code>/task</code></td>
      <td style="padding:8px 12px;color:#8888a0;">Two-phase task orchestration</td>
    </tr>
    <tr style="border-bottom:1px solid rgba(255,255,255,0.04);">
      <td style="padding:8px 12px;"><code>/budget</code></td>
      <td style="padding:8px 12px;color:#8888a0;">Daily budget management</td>
    </tr>
    <tr style="border-bottom:1px solid rgba(255,255,255,0.04);">
      <td style="padding:8px 12px;"><code>/cost</code></td>
      <td style="padding:8px 12px;color:#8888a0;">Cost summary and breakdown</td>
    </tr>
    <tr style="border-bottom:1px solid rgba(255,255,255,0.04);">
      <td style="padding:8px 12px;"><code>/voice</code></td>
      <td style="padding:8px 12px;color:#8888a0;">Voice reply settings + preview</td>
    </tr>
    <tr style="border-bottom:1px solid rgba(255,255,255,0.04);">
      <td style="padding:8px 12px;"><code>/el</code></td>
      <td style="padding:8px 12px;color:#8888a0;">ElevenLabs account dashboard</td>
    </tr>
    <tr style="border-bottom:1px solid rgba(255,255,255,0.04);">
      <td style="padding:8px 12px;"><code>/cron</code></td>
      <td style="padding:8px 12px;color:#8888a0;">Scheduled tasks</td>
    </tr>
    <tr style="border-bottom:1px solid rgba(255,255,255,0.04);">
      <td style="padding:8px 12px;"><code>/new</code></td>
      <td style="padding:8px 12px;color:#8888a0;">Reset current session</td>
    </tr>
    <tr>
      <td style="padding:8px 12px;"><code>/restart</code></td>
      <td style="padding:8px 12px;color:#8888a0;">Restart CB service</td>
    </tr>
  </tbody>
</table>

<hr style="border:none;border-top:1px solid rgba(255,255,255,0.06);margin:32px 0;"/>

<p style="color:#4a4a60;font-size:13px;">
Claude Bridge v1.5.0 &middot; Open source on <a href="https://github.com/dharmaxis-group/claude-bridge">GitHub</a>
</p>

</body>
</html>
