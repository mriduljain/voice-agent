# ruff: noqa
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import datetime
from zoneinfo import ZoneInfo

from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.models import Gemini
from google.genai import types
#from .instructions import VOICE_INSTRUCTIONS
from .business_config import BUSINESS_CONFIG
from .instructions import build_instructions
import os
from pathlib import Path
from dotenv import load_dotenv

# agent.py is inside app/; .env is in the project root.
env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(env_path)

root_agent = Agent(
    # Keep in sync with agents-cli-manifest.yaml: agents-cli derives this name
    # from the project `name:` recorded there, and telemetry reports it as
    # gen_ai.agent.name. Renaming the agent only here makes the two disagree,
    # and anything selecting traces by name stops finding this agent's.
    name="voice_agent",
    model=os.getenv(
        "VOICE_MODEL",
        "gemini-live-2.5-flash-native-audio",
    ),
    instruction=build_instructions(BUSINESS_CONFIG),
)

app = App(
    root_agent=root_agent,
    name="app",
)
