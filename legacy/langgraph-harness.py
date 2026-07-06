import os
import subprocess
import re
from typing import List, Literal, Optional, TypedDict
from pydantic import BaseModel, Field
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage
from langgraph.graph import StateGraph, END
from openai import OpenAI

DEBUG_MODE = True
WORKSPACE = "project_workspace"

try:
    temp_client = OpenAI(base_url="http://localhost:8080/v1", api_key="not-needed")
    AVAILABLE_MODELS = [m.id for m in temp_client.models.list().data]
    ACTIVE_MODEL = AVAILABLE_MODELS[0] if AVAILABLE_MODELS else "default"
    print(f"🔌 Server Model Detected: {ACTIVE_MODEL}")
except Exception as e:
    ACTIVE_MODEL = "default"
    print("⚠️ Could not fetch available models from server. Using 'default'.")

# Streaming LLM for execution tasks with a hard cutoff to prevent runaway generation
llm = ChatOpenAI(
    base_url="http://localhost:8080/v1",
    api_key="not-needed",
    model=ACTIVE_MODEL,
    temperature=0.1,
    max_tokens=2000, # Hard cap
    streaming=True
)

# Structured LLM for PM JSON generation (NO STREAMING)
structured_llm = ChatOpenAI(
    base_url="http://localhost:8080/v1",
    api_key="not-needed",
    model=ACTIVE_MODEL,
    temperature=0.1,
    max_tokens=2000 # Hard cap for JSON parsing
)

def setup_workspace():
    os.makedirs(f"{WORKSPACE}/src", exist_ok=True)
    os.makedirs(f"{WORKSPACE}/tests", exist_ok=True)
    os.makedirs(f"{WORKSPACE}/reviews", exist_ok=True)
    open(f"{WORKSPACE}/src/__init__.py", "w").close()
    open(f"{WORKSPACE}/tests/__init__.py", "w").close()
    with open(f"{WORKSPACE}/src/implementation.py", "w") as f:
        f.write("# Initial empty implementation\n")

class AgentFocus(BaseModel):
    role_name: str = Field(description="Name of the role")
    focus_area: str = Field(description="What this specific agent should focus on.")

class StagePlan(BaseModel):
    stage_name: str = Field(description="Name of the project stage")
    description: str = Field(description="What needs to be accomplished in this stage")
    test_designers: List[AgentFocus]
    implementers: List[AgentFocus]
    reviewers: List[AgentFocus]
    loop_allowance: int = Field(description="Number of attempts the developer gets. ALWAYS set to 3.", default=3)

class ProjectPlan(BaseModel):
    stages: List[StagePlan]

class RequirementAssessment(BaseModel):
    is_clear: bool = Field(description="Are the requirements technical and detailed enough to begin coding?")
    question: str = Field(description="If not clear, ask the user. If clear, leave empty.", default="")

class PMDecision(BaseModel):
    decision: Literal["advance_stage", "retry_dev", "retry_tests", "abort"]
    reasoning: str

class AgencyState(TypedDict):
    project_goal: str
    is_clear: bool
    specification: str
    plan: ProjectPlan
    current_stage_idx: int
    current_stage: StagePlan
    test_code: str
    impl_code: str
    test_errors: str
    reviews: str
    loop_count: int
    pm_decision: str

def stream_llm(sys_prompt: str, user_prompt: str, role: str) -> str:
    if DEBUG_MODE:
        print(f"\n\033[90m[DEBUG: {role} SYSTEM PROMPT]\n{sys_prompt}\033[0m")
        print(f"\033[90m[DEBUG: {role} USER PROMPT]\n{user_prompt}\033[0m\n")

    print(f"[{role} TYPING] ", end="", flush=True)
    full_response = ""
    for chunk in llm.stream([SystemMessage(content=sys_prompt), HumanMessage(content=user_prompt)]):
        content = chunk.content
        print(content, end="", flush=True)
        full_response += content
    print("\n")
    return full_response

def pm_requirements_node(state: AgencyState):
    print("\n👔 [PM] Reviewing requirements...")
    pm_eval_llm = structured_llm.with_structured_output(RequirementAssessment)
    prompt = (
        f"Review this project goal: {state['project_goal']}\n"
        "CRITICAL: A goal is ONLY clear if it defines specific function names, exact inputs, and expected return types. "
        "Broad requests like 'a hello world script' are NEVER clear enough. "
        "If it lacks technical specifics, set is_clear to False and ask the user a specific question to clarify."
    )
    assessment: RequirementAssessment = pm_eval_llm.invoke([HumanMessage(content=prompt)])

    if assessment.is_clear:
        print("   -> PM is satisfied with the requirements.")
        return {"is_clear": True}
    else:
        print(f"\n👔 [PM]: {assessment.question}")
        answer = input("🗣️  [YOU]: ")
        new_goal = state['project_goal'] + f"\n\nPM asked: {assessment.question}\nUser answered: {answer}"
        return {"project_goal": new_goal, "is_clear": False}

def pm_spec_node(state: AgencyState):
    print("\n👔 [PM] Writing Technical Specification...")
    sys_prompt = "You are a Technical Product Manager. Write a clear, concise technical specification defining the exact function names, input arguments, and return types to be built."
    user_prompt = f"Project Goal: {state['project_goal']}\nWrite the markdown specification."
    spec = stream_llm(sys_prompt, user_prompt, "PM SPEC WRITER")

    with open(f"{WORKSPACE}/README.md", "w") as f:
        f.write(spec)
    return {"specification": spec}

def pm_planner_node(state: AgencyState):
    print("\n👔 [PM] Planning Project Stages and Team Allocation...")
    pm_plan_llm = structured_llm.with_structured_output(ProjectPlan)
    prompt = (
        f"You are the Lead Technical PM. Break this spec down into the MINIMUM number of coding stages required.\n"
        f"CRITICAL: A 'stage' must be a specific code component to build (e.g., 'Core Logic', 'CLI Interface').\n"
        f"NEVER use terms like 'Requirements Gathering' or 'Planning'.\n"
        f"Specification: {state['specification']}"
    )
    plan: ProjectPlan = pm_plan_llm.invoke([HumanMessage(content=prompt)])
    print(f"   -> PM defined {len(plan.stages)} stages: {[s.stage_name for s in plan.stages]}")

    return {
        "plan": plan,
        "current_stage_idx": 0,
        "current_stage": plan.stages[0],
        "loop_count": 0,
        "test_code": "",
        "impl_code": "",
        "test_errors": "",
        "reviews": "",
        "pm_decision": ""
    }

def test_team_node(state: AgencyState):
    print(f"\n🧪 [TEST TEAM] Designing Tests for Stage: {state['current_stage'].stage_name}")
    stage = state['current_stage']

    sys_prompt = (
        "You are the Test Architecture Team. Output ONLY raw Python code wrapped in ```python ... ```.\n"
        "You MUST include `import pytest` and `from src.implementation import *` at the top of your code.\n"
        "CRITICAL: Do NOT write endless permutations of the same edge case.\n"
        "If you are reviewing feedback or an existing test suite, and you decide the current tests are completely sufficient "
        "and need no additions to meet the specification, output EXACTLY the word: SUFFICIENT"
    )

    user_prompt = (
        f"Project Specification:\n{state['specification']}\n\n"
        f"Stage Goal: {stage.description}\n"
    )

    if state.get('test_code'):
        user_prompt += f"Current Tests:\n{state['test_code']}\n\n"
    if state.get('impl_code'):
        user_prompt += f"Previous Implementation:\n{state['impl_code']}\n\n"
    if state.get('reviews'):
        user_prompt += f"Reviewer Feedback:\n{state['reviews']}\n\n"

    user_prompt += "Write the pytest suite now, or output SUFFICIENT if the current tests need no changes."

    raw_response = stream_llm(sys_prompt, user_prompt, "TEST ARCHITECT")

    if "SUFFICIENT" in raw_response.upper() and state.get("test_code"):
        print("   -> Test Architect determined existing tests are SUFFICIENT. No changes made.")
        return {"test_code": state["test_code"]}

    new_tests = raw_response.split("```python")[1].split("```")[0].strip() if "```python" in raw_response else raw_response

    with open(f"{WORKSPACE}/tests/test_suite.py", "w") as f:
        f.write(new_tests)

    return {"test_code": new_tests}

def dev_team_node(state: AgencyState):
    print(f"\n💻 [DEV TEAM] Implementing Stage: {state['current_stage'].stage_name} (Loop {state['loop_count'] + 1})")
    stage = state['current_stage']

    sys_prompt = "You are the Implementation Team. Output ONLY raw Python code wrapped in ```python ... ```."
    user_prompt = (
        f"Project Specification:\n{state['specification']}\n\n"
        f"Stage Goal: {stage.description}\n"
        f"Current Tests:\n{state['test_code']}\n"
    )
    if state.get('test_errors'):
        user_prompt += f"Test Errors to fix:\n{state['test_errors']}\n\n"

    user_prompt += "Write the implementation to pass these tests."

    raw_response = stream_llm(sys_prompt, user_prompt, "DEVELOPER")
    new_code = raw_response.split("```python")[1].split("```")[0].strip() if "```python" in raw_response else raw_response

    with open(f"{WORKSPACE}/src/implementation.py", "w") as f:
        f.write(new_code)

    env = os.environ.copy()
    env["PYTHONPATH"] = os.path.abspath(WORKSPACE)
    result = subprocess.run(["python3", "-m", "pytest", f"{WORKSPACE}/tests/"], env=env, capture_output=True, text=True)

    raw_errors = result.stdout + result.stderr if result.returncode != 0 else ""
    # TRUNCATE PYTEST OUTPUT TO PREVENT CONTEXT OVERFLOW
    if len(raw_errors) > 1500:
        raw_errors = "...[TRUNCATED]...\n" + raw_errors[-1500:]

    print(f"   -> Pytest Result: {'PASS' if result.returncode == 0 else 'FAIL'}")
    return {"impl_code": new_code, "test_errors": raw_errors, "loop_count": state["loop_count"] + 1}

def review_team_node(state: AgencyState):
    print("\n🔍 [REVIEW TEAM] Analyzing passing code...")
    stage = state['current_stage']
    all_reviews = ""

    for reviewer in stage.reviewers:
        sys_prompt = f"You are a {reviewer.role_name}. Focus on: {reviewer.focus_area}. If perfect, output PASS. Otherwise, provide actionable code feedback."
        user_prompt = f"Spec:\n{state['specification']}\n\nTests:\n{state['test_code']}\n\nCode:\n{state['impl_code']}"

        resp = stream_llm(sys_prompt, user_prompt, f"REVIEWER: {reviewer.role_name}")
        # Strip trailing JSON hallucinations
        resp = re.sub(r'```json\s*\{.*?"name":.*?\}.*?```', '', resp, flags=re.DOTALL | re.IGNORECASE).strip()

        all_reviews += f"[{reviewer.role_name}]: {resp}\n\n"

        with open(f"{WORKSPACE}/reviews/{reviewer.role_name.replace(' ', '_')}.txt", "w") as f:
            f.write(resp)

    return {"reviews": all_reviews}

def pm_evaluator_node(state: AgencyState):
    print("\n👔 [PM EVALUATOR] Assessing project state...")
    pm_eval_llm = structured_llm.with_structured_output(PMDecision)

    prompt = (
        f"The development team failed the tests {state['loop_count']} times.\n"
        f"Latest test errors:\n{state['test_errors']}\n"
        "Should we give the developer another try ('retry_dev'), rewrite the tests ('retry_tests'), or advance?"
    )
    decision: PMDecision = pm_eval_llm.invoke([HumanMessage(content=prompt)])
    print(f"   -> PM Decision: {decision.decision} (Reason: {decision.reasoning})")

    # CRITICAL FIX: We must reset loop_count back to 0 here so the dev actually gets their attempts back!
    return {"pm_decision": decision.decision, "loop_count": 0}

def advance_stage_node(state: AgencyState):
    next_idx = state["current_stage_idx"] + 1
    next_stage = state["plan"].stages[next_idx]
    print(f"\n⏩ Advancing to Stage {next_idx + 1}: {next_stage.stage_name}")
    return {
        "current_stage_idx": next_idx,
        "current_stage": next_stage,
        "loop_count": 0,
        "test_errors": "",
        "reviews": ""
    }

def route_after_requirements(state: AgencyState):
    return "pm_spec_node" if state.get("is_clear") else "pm_requirements_node"

def route_after_dev(state: AgencyState):
    if not state.get("test_errors"):
        return "review_team_node"
    if state["loop_count"] >= state["current_stage"].loop_allowance:
        return "pm_evaluator_node"
    return "dev_team_node"

def route_after_review(state: AgencyState):
    if "FAIL" in state.get("reviews", "") or "missing" in state.get("reviews", "").lower():
        return "test_team_node"
    next_idx = state["current_stage_idx"] + 1
    if next_idx < len(state["plan"].stages):
        return "advance_stage_node"
    return END

def route_after_evaluator(state: AgencyState):
    decision = state.get("pm_decision", "abort")
    if decision == "retry_tests":
        return "test_team_node"
    elif decision == "retry_dev":
        return "dev_team_node"
    elif decision == "advance_stage":
        next_idx = state["current_stage_idx"] + 1
        if next_idx < len(state["plan"].stages):
            return "advance_stage_node"
    return END

workflow = StateGraph(AgencyState)

workflow.add_node("pm_requirements_node", pm_requirements_node)
workflow.add_node("pm_spec_node", pm_spec_node)
workflow.add_node("pm_planner_node", pm_planner_node)
workflow.add_node("test_team_node", test_team_node)
workflow.add_node("dev_team_node", dev_team_node)
workflow.add_node("review_team_node", review_team_node)
workflow.add_node("pm_evaluator_node", pm_evaluator_node)
workflow.add_node("advance_stage_node", advance_stage_node)

workflow.set_entry_point("pm_requirements_node")

workflow.add_conditional_edges("pm_requirements_node", route_after_requirements)
workflow.add_edge("pm_spec_node", "pm_planner_node")
workflow.add_edge("pm_planner_node", "test_team_node")
workflow.add_edge("test_team_node", "dev_team_node")
workflow.add_conditional_edges("dev_team_node", route_after_dev)
workflow.add_conditional_edges("review_team_node", route_after_review)
workflow.add_conditional_edges("pm_evaluator_node", route_after_evaluator)
workflow.add_edge("advance_stage_node", "test_team_node")

app = workflow.compile()

if __name__ == "__main__":
    setup_workspace()
    goal = input("Enter the project goal: ")
    initial_state = {"project_goal": goal, "is_clear": False, "loop_count": 0, "test_errors": "", "reviews": ""}

    for output in app.stream(initial_state):
        pass
    print("\n🎉 Project Complete!")

