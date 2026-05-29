# Why Claude Bridge?

<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body>
<h2>Why Claude Bridge? — A New Way to Control Your Mac with AI</h2>

<p>If you're a Claude Max subscriber, you're paying $100/month for one of the most powerful AI coding assistants available. But there's a catch: <strong>you can only use it while sitting in front of your computer</strong>.</p>

<p>Claude Bridge changes that.</p>

<hr/>

<h3>The Problem with Traditional Remote Control</h3>

<p>Most remote access solutions require significant setup and have real limitations:</p>

<ul>
  <li><strong>SSH</strong> — requires a public IP or port forwarding, exposes your machine to the internet</li>
  <li><strong>Tailscale</strong> — great tool, but in China it conflicts with VPN proxies (you can't run both smoothly)</li>
  <li><strong>VNC / TeamViewer</strong> — heavy, slow, blocked by GFW in China</li>
  <li><strong>Remote Desktop</strong> — requires the screen to be on, consumes resources</li>
</ul>

<p>All of these solve the same problem the same way: <em>give you a window into your computer</em>. But that's not actually what most developers need.</p>

<hr/>

<h3>What You Actually Need</h3>

<p>Most of the time, you don't need to <em>see</em> your computer. You need to <em>tell it to do something</em>.</p>

<blockquote>
"Check if the service is still running."<br/>
"Look at the logs and tell me if there are any errors."<br/>
"Deploy the fix we discussed earlier."<br/>
"Run the tests and send me the results."
</blockquote>

<p>These are <strong>instructions</strong>, not mouse clicks. And Claude Code is already perfect at executing them — it just needs a way to receive them from anywhere.</p>

<p>That's exactly what Claude Bridge does.</p>

<hr/>

<h3>How It Works</h3>

<pre>
Your Phone (Telegram)
       ↓
  Claude Bridge Bot
       ↓
  Claude Code CLI  ←  reads your project's CLAUDE.md
       ↓
  Your Mac (local execution)
       ↓
  Result → back to Telegram
</pre>

<p>You send a message to your Telegram bot. Claude Bridge passes it to Claude Code running locally on your Mac. Claude Code reads, thinks, executes — then sends the result back to your phone.</p>

<p>No public IP. No port forwarding. No VPN conflicts. <strong>Just Telegram.</strong></p>

<hr/>

<h3>The China User Advantage</h3>

<p>For users in China, this is particularly significant.</p>

<p>The typical setup is: VPN proxy (like mihomo/clash) for internet access + Tailscale for device connectivity. The problem? <strong>They conflict</strong>. Running both simultaneously causes routing issues, dropped connections, and unstable performance.</p>

<p>Claude Bridge sidesteps this entirely:</p>
<ul>
  <li>Telegram traffic goes through your existing proxy (mihomo port 1082)</li>
  <li>Claude Code runs locally — no outbound connection needed</li>
  <li>No Tailscale required at all</li>
  <li>One proxy, one connection, everything works</li>
</ul>

<p>As long as you can open Telegram, you have full access to Claude Code on your Mac.</p>

<hr/>

<h3>A New Interaction Paradigm</h3>

<p>Traditional remote control: <em>you control the computer</em><br/>
Claude Bridge: <em>you instruct an AI, which controls the computer</em></p>

<p>This is the difference between driving a car and telling a driver where to go. You don't need to know the exact commands. You don't need to remember the file paths. You just describe what you want.</p>

<ul>
  <li>✅ "Check if <redacted> is running and healthy"</li>
  <li>✅ "Find any Python errors in today's logs"</li>
  <li>✅ "Run the deployment script for the new feature"</li>
  <li>✅ "What's the current disk usage on my Mac?"</li>
</ul>

<p>Claude Code figures out how. You get the answer on your phone.</p>

<hr/>

<h3>Zero Extra Cost</h3>

<p>If you already have Claude Max ($100/month), Claude Bridge costs nothing additional. It uses the same <code>claude</code> CLI that you're already paying for. The bridge itself is just I/O — all the intelligence comes from Claude Code.</p>

<p>No API keys. No separate billing. No usage limits beyond what your Max subscription provides.</p>

<hr/>

<h3>Real Use Cases</h3>

<p><strong>Away from desk:</strong> You're in a meeting and remember you need to check if a cron job ran. Pull out your phone, ask the bot. Done in 30 seconds without leaving the room.</p>

<p><strong>Traveling:</strong> You're in a different timezone and your automated system needs a fix. Open Telegram, describe the issue, Claude Code patches it while you're on the train.</p>

<p><strong>Multi-project management:</strong> Register multiple projects with <code>/p add</code>. Switch between your personal infrastructure, work projects, and side projects — all from the same Telegram conversation.</p>

<p><strong>Peace of mind:</strong> Before sleeping, ask "is everything running normally?" Get a quick health check without opening your laptop.</p>

<hr/>

<h3>Get Started</h3>

<p>Claude Bridge is open source. If you have Claude Max and a Mac, you have everything you need.</p>

<p>→ <a href="https://github.com/dharmaxis-group/claude-bridge">GitHub: dharmaxis-group/claude-bridge</a><br/>
→ <a href="https://<removed>"><removed></a></p>

<p><em>Requirements: macOS, Claude Max subscription, Telegram account, Python 3.10+</em></p>
</body>
</html>
