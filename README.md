# 🌴 SpaceXAI Miami Community QA Hub 💥

> **Autonomous multi-agent QA testing playground built with Grok Bot for the SpaceXAI Miami Community.**  
> *"Because shipping buggy code that crashes the moment an investor opens it is not very Miami Tech."*

Welcome to the **SpaceXAI Miami Community QA Hub**—a public, multi-agent autonomous playground built using our **Grok Bot** credits to act as an instant safety net for early-stage builders. 

Instead of a boring text wrapper, this app spins up a headless browser cluster inside a persistent cloud Linux container to ruthlessly assault, audit, and wiretap your staging URLs before you push them to production.

---

## ⚡ The Team (Multi-Agent Architecture)

We don't do basic unit tests. When you paste a URL and hit **Test App**, a specialized three-agent strike team deploys into the viewport:

*   **🕵️‍♂️ Agent A: The Chaos Client**  
    Mimics your absolute worst user. It triggers rapid-fire rage clicks, fires empty form payloads, executes chaotic keyboard tab-storms, and tries to force race conditions to break your frontend state variables.
    
*   **🩺 Agent B: The DOM Auditor**  
    Our structural compliance inspector. It goes under the hood into the browser's raw Document Object Model (DOM) tree. It audits your layout elements against international accessibility guidelines (WCAG) and flags hidden visual overlap collisions that render elements unclickable.
    
*   **🔌 Agent C: The Console Detective**  
    The silent wiretapper. It hooks directly into the browser runtime's console logs and network traffic, capturing silent JavaScript exceptions, unhandled promises, and 400/500 API payload crashes that developers miss.

---

## 📊 The Gamified "Resiliency Score Card"

Every staging surface starts with a flawless **100/100 Stability Score**. The agents then deduct points based on the severity of flaws discovered:
*   `-12 points` per network crash or dead API endpoint.
*   `-11 points` per critical structural or accessibility violation.
*   `-4 points` per layout collision or form handling error.

The result? A localized, interactive dashboard with **live telemetry streams**, a dynamic scoring ring widget, an automated **Actionable Developer Fixes** engine, and a **Download PDF Report** system for quick distribution.

---

## 🛠️ Local Tech Stack & Quickstart

*   **Backend:** FastAPI (Python) for asynchronous agent task orchestration.
*   **Automation Cluster:** Playwright (Headless Chromium engine).
*   **Frontend UX:** Modern "Miami Cyberpunk" Dark-Mode Theme powered by Tailwind CSS.

### 🏃‍♂️ Running It Locally
Ensure your environment variables are configured, then initialize the pipeline:

```bash
# 1. Install the core infrastructure dependencies
pip install -r requirements.txt

# 2. Provision the headless browser engines
playwright install chromium

# 3. Ignite the dashboard server
uvicorn app:app --reload
```

Open `http://127.0.0.1:8000` and start stress-testing!

---

## 🏆 Created for the SpaceXAI Miami Tech Competition
Built with ❤️ by a QA Engineer looking to accelerate shipping velocity across the Miami builder ecosystem. Remember: Production is a sacred place. Protect it.
