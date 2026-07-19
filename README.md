# LocalLink
<img width="1408" height="768" alt="image" src="https://github.com/user-attachments/assets/696889cb-674c-4860-b5ab-f1d6ccf86065" />

*   **Note: ** This is still underdevelopment, where the design and technologies we are using might change. 

An offline-first, decentralized P2P messaging engine featuring automated, asynchronous syncing to the federated Matrix network.

## 📌 Overview

**LocalLink** is a resilient communication framework built to keep people connected when centralized internet infrastructure fails or becomes compromised. When standard networks are down, the tool establishes a secure, local peer-to-peer (P2P) mesh network over an available Wi-Fi connection. 

The moment *any* single device within the local mesh regains internet access, it automatically acts as a gateway bridge. It checks the local database for unsynced offline message queues and updates them directly into a federated Matrix room—ensuring the conversation seamlessly flows onto the global open web without forcing users to rely on closed, centralized platforms.

## 🚀 Core Features

*   **Zero-Config Discovery:** Leverages mDNS (Zeroconf) to automatically identify and pair local nodes without a central router or server.
*   **Offline-First Architecture:** Ensures absolute data sovereignty. Every individual user retains a local, serverless message ledger.
*   **End-to-End Cryptography:** Integrated via PyNaCl to encrypt all local communication payloads, safeguarding local Wi-Fi traffic from snooping or side-channel inspection.
*   **Asynchronous Matrix Bridge:** A background syncing worker that detects internet restoration, handles delta updates, and reflects offline local rooms into mainstream open standards.
*   **Modular Architecture:** Structured explicitly as an independent headless engine. Developers can easily decouple the system core to power custom terminal apps, web panels, or native modules.

## 🎯 Target Scenarios

1.  **Emergency Infrastructure Failures:** Providing immediate communication channels during natural disasters or power grid collapses.
2.  **High-Density Gridlock:** Bypassing standard cellular tower congestion during large festivals, stadium events, or civil demonstrations.
3.  **Structural Dead Zones:** Constructing ad-hoc connectivity webs inside campus basements, subway lines, or rural spaces.
4.  **Digital Sovereignty Advocates:** Facilitating local-first networks for groups requiring secure communication completely decoupled from corporate telemetry.

## 🏗 Architecture Layers

### 1. Local Mesh Layer (The Engine)
The core component of the project. It handles automatic peer broadcasting and inbound/outbound local synchronization routes. It uses a clean, headless API structure designed to run independently from visual interfaces.

### 2. Storage Layer (The Ledger)
A standalone, local-first system that ensures messages are securely committed to the device. It records chat histories, neighboring peer indices, and distinct synchronization tracking states (`synced = true/false`).

### 3. Bridge Layer (The Gateway)
An intelligent client wrapper connecting your local network to the global **Matrix Protocol**. It watches local system interfaces for internet routes and safely pipes queued logs to Matrix rooms via REST APIs, allowing standard Matrix clients like **Element** to join the chat stream.

### 4. Presentation Layer (The UI Demo)
Handles all user interaction, text rendering, and view state management. Because it interacts solely with the engine's public APIs, you can swap between the immersive terminal UI or the browser demo seamlessly without rewriting core logic.

## 🛠 Tech Stack

| Layer | Component / Technology | Operational Role |
| :--- | :--- | :--- |
| **Presentation** | Textual (TUI) / HTML & JS | Visual environments for user interaction and interface control |
| **Global Federation** | Matrix Protocol | Open, decentralized network standard for global message sync |
| **Local Mesh Networking** | mDNS (Zeroconf) + HTTP/REST | Dynamic peer discovery and point-to-point message transmission |
| **Application Core** | Python / Flask | High-speed local API routing and modular engine processing |
| **Data Persistence** | SQLite | Serverless, file-based database for complete local data ownership |
| **Security Layer** | PyNaCl (libsodium) | Authenticated public-key cryptography for end-to-end security |

## 📁 Repository Layout

```text
/LocalLink
├── engine/                     # Standalone headless core backend
│   ├── mesh/                   # P2P discovery & local network routing
│   │   ├── discovery.py        # mDNS broadcasting & listener loops
│   │   └── server.py           # Flask instance handling P2P traffic
│   ├── network/
│   │   └── client.py           # Outbound HTTP POST payload dispatchers
│   ├── storage/
│   │   ├── database.py         # Transaction handlers & query abstractions
│   │   └── models.py           # Messaging schema & sync state flags
│   └── security/
│       ├── crypto.py           # PyNaCl end-to-end encryption routines
│       └── keys.py             # Public/private keypair lifecycle tools
├── bridge/                     # Asynchronous Matrix integration layer
│   ├── matrix_sync.py          # Queue monitoring & synchronization worker
│   └── config.json             # Matrix credentials, targets & credentials
├── cli/                        # Rich Terminal User Interface (TUI)
│   ├── components/             # Modular TUI layout widgets (Chat boxes, sidebars)
│   ├── views/                  # Screen managers (Onboarding, chat viewports)
│   └── main.py                 # Interface runtime script
├── web/                        # Embedded Web Interface Demo
│   ├── static/                 # Front-end asset pipelines (CSS/JS modules)
│   └── server.py               # Flask pipeline presenting the browser UI
├── requirements.txt            # Dependency listings
└── run.py                      # Master execution entrypoint

**[To Do]**

## 📊 Development Status & Progress Tracker

### 🟠 Phase 1: Core Engine & Architecture [In Progress]

### ⚪ Phase 2: Presentation Layer & TUI

### ⚪ Phase 3: The Matrix Gateway Bridge

### ⚪ Phase 4: Verification & Microcontroller Exploration
