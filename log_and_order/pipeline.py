"""Coordinate extraction, the optional model, and the chosen report."""

import sys
from pathlib import Path

import httpx
from langchain_text_splitters import RecursiveCharacterTextSplitter
from ollama import ResponseError

from .config import CHUNK_OVERLAP, CHUNK_SIZE, MODEL_NAME
from .incidents.builder import build_incident_report
from .incidents.jenkins import build_jenkins_report
from .llm.client import GenerationError, build_llm
from .llm.enrichment import disabled_enrichment, enrich_incident, fallback_enrichment
from .llm.prompts import COMBINE_PROMPT, JENKINS_REPORT_PROMPT, SUMMARIZE_PROMPT
from .llm.summarization import interpret_events, summarize_chunks
from .logs.extraction import extract_events
from .logs.jenkins.analysis import JenkinsAnalysis, analyse_jenkins
from .logs.selection import independent_events, select_primary
from .models import Event, IncidentEnrichment, IncidentReport
from .reports.agent import render_agent_report
from .reports.human import compose_report, fallback_narrative, fallback_report
from .reports.jenkins import format_jenkins_model_input, render_jenkins_human
from .reports.json import render_json_report, report_payload


class FormatError(Exception):
    """An agent or JSON report was requested for a file that is not a log."""


def format_agent_report(
    path: Path,
    text: str,
    events: list[Event],
    enrichment: IncidentEnrichment | None = None,
    jenkins: JenkinsAnalysis | None = None,
) -> str:
    """Markdown brief for a developer and for a coding agent. One incident, told once."""
    if jenkins is not None:
        report = build_jenkins_report(path, jenkins)
    else:
        report = build_incident_report(path, text, events)
    return render_agent_report(report, enrichment or disabled_enrichment())


def format_json_report(
    path: Path,
    text: str,
    events: list[Event],
    enrichment: IncidentEnrichment | None = None,
    jenkins: JenkinsAnalysis | None = None,
) -> str:
    """The same incident as the Markdown brief, as JSON."""
    if jenkins is not None:
        report = build_jenkins_report(path, jenkins)
    else:
        report = build_incident_report(path, text, events)
    return render_json_report(report, enrichment or disabled_enrichment())


def _report_and_enrichment(
    path: Path,
    text: str,
    events: list[Event],
    *,
    use_llm: bool,
    model: str | None,
    base_url: str | None = None,
    timeout: float | None = None,
    jenkins: JenkinsAnalysis | None = None,
) -> tuple[IncidentReport, IncidentEnrichment]:
    """Build one incident and the optional analysis. The model cannot replace the facts."""
    if jenkins is not None:
        report = build_jenkins_report(path, jenkins)
        primary = jenkins.primary_event
    else:
        report = build_incident_report(path, text, events)
        primary = select_primary(events)
    if not use_llm:
        enrichment = disabled_enrichment()
    elif primary is None:
        enrichment = fallback_enrichment("no_incident")
    else:
        try:
            llm = build_llm(
                reasoning=False,
                num_predict=1024,
                model=model,
                json_mode=True,
                base_url=base_url,
                timeout=timeout,
            )
            enrichment = enrich_incident(report, primary, llm)
        except (
            httpx.ConnectError,
            httpx.TimeoutException,
            ResponseError,
            GenerationError,
            ConnectionError,
        ) as exc:
            print(
                f"Enrichment failed ({exc}). Writing the deterministic incident report.",
                file=sys.stderr,
            )
            enrichment = fallback_enrichment("unavailable", model or MODEL_NAME)
    return report, enrichment


def _agent_or_json(
    path: Path,
    text: str,
    events: list[Event],
    report_format: str,
    *,
    use_llm: bool,
    model: str | None,
    base_url: str | None = None,
    timeout: float | None = None,
    jenkins: JenkinsAnalysis | None = None,
) -> str:
    """One incident document. The model is optional and cannot replace the facts."""
    report, enrichment = _report_and_enrichment(
        path,
        text,
        events,
        use_llm=use_llm,
        model=model,
        base_url=base_url,
        timeout=timeout,
        jenkins=jenkins,
    )
    if report_format == "json":
        return render_json_report(report, enrichment)
    return render_agent_report(report, enrichment)


def incident_payload(
    path: Path,
    text: str,
    *,
    use_llm: bool = False,
    model: str | None = None,
    base_url: str | None = None,
    timeout: float | None = None,
) -> dict:
    """The JSON incident document. Raises FormatError when the text is not a log.

    Jenkins is detected first, including consoles that have no timestamps.
    A timestamped log with no kept events still returns the empty incident.
    use_llm false leaves the analysis disabled. A model that cannot be reached
    still returns the factual report.
    """
    jenkins = analyse_jenkins(text)
    if jenkins is not None:
        report, enrichment = _report_and_enrichment(
            path,
            "",
            jenkins.events,
            use_llm=use_llm,
            model=model,
            base_url=base_url,
            timeout=timeout,
            jenkins=jenkins,
        )
        return report_payload(report, enrichment)
    events = extract_events(text)
    if events is None:
        raise FormatError("Agent and JSON reports require a timestamped log.")
    report, enrichment = _report_and_enrichment(
        path,
        text,
        events,
        use_llm=use_llm,
        model=model,
        base_url=base_url,
        timeout=timeout,
    )
    return report_payload(report, enrichment)


def summarize(
    path: Path,
    text: str,
    report_format: str = "human",
    *,
    use_llm: bool = True,
    model: str | None = None,
    base_url: str | None = None,
    timeout: float | None = None,
) -> str:
    """Report a Jenkins console or a timestamped log, or summarize other text.

    Jenkins is detected first, including consoles that have no timestamps.
    The human log path extracts events here and asks the model only to narrate
    them. Agent and JSON reports use the same events and, unless use_llm is
    false, add a validated diagnostic analysis. Fewer than five timestamped
    records, and a file that is not Jenkins, is treated as ordinary text.
    """
    chosen = model or MODEL_NAME
    jenkins = analyse_jenkins(text)
    if jenkins is not None:
        return _summarize_jenkins(
            path,
            jenkins,
            report_format,
            use_llm=use_llm,
            model=model,
            base_url=base_url,
            timeout=timeout,
        )
    events = extract_events(text)
    if events is not None:
        # A timestamped log with nothing kept still has a path: human says so in one line,
        # and agent or JSON still emit the empty incident document.
        if not events:
            if report_format == "human":
                return "No relevant errors or warnings were found."
            return _agent_or_json(
                path,
                text,
                events,
                report_format,
                use_llm=use_llm,
                model=model,
                base_url=base_url,
                timeout=timeout,
            )
        print(
            f"Extracted {len(events)} events from {len(text.splitlines())} lines.",
            file=sys.stderr,
        )
        if report_format in {"agent", "json"}:
            return _agent_or_json(
                path,
                text,
                events,
                report_format,
                use_llm=use_llm,
                model=model,
                base_url=base_url,
                timeout=timeout,
            )
        primary = select_primary(events)
        independent = independent_events(events, primary)
        if not use_llm or primary is None:
            return fallback_report(primary, independent)
        try:
            narrative = interpret_events(
                path,
                primary,
                model,
                base_url=base_url,
                timeout=timeout,
            )
        except GenerationError:
            print(
                "The model did not produce a summary. Using the extracted events.",
                file=sys.stderr,
            )
            narrative = fallback_narrative(primary)
        return compose_report(narrative, independent)
    # Ordinary text. Agent and JSON have no incident to describe, and the
    # human summary is a model call.
    if report_format != "human":
        raise FormatError("Agent and JSON reports require a timestamped log.")
    if not use_llm:
        raise FormatError("A non-log summary requires the model.")

    print(f"Summarizing {path} with {chosen}...", file=sys.stderr)
    llm = build_llm(model=model, base_url=base_url, timeout=timeout)
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
    )
    return summarize_chunks(
        path,
        splitter.split_text(text),
        piece_prompt=SUMMARIZE_PROMPT,
        combine_prompt=COMBINE_PROMPT,
        llm=llm,
    )


def _summarize_jenkins(
    path: Path,
    jenkins: JenkinsAnalysis,
    report_format: str,
    *,
    use_llm: bool,
    model: str | None,
    base_url: str | None = None,
    timeout: float | None = None,
) -> str:
    """Report a Jenkins console log through the same human, agent, and JSON outputs."""
    print(
        f"Extracted {len(jenkins.events)} events from {len(jenkins.lines)} lines.",
        file=sys.stderr,
    )
    if report_format in {"agent", "json"}:
        return _agent_or_json(
            path,
            "",
            jenkins.events,
            report_format,
            use_llm=use_llm,
            model=model,
            base_url=base_url,
            timeout=timeout,
            jenkins=jenkins,
        )
    report = build_jenkins_report(path, jenkins)
    if jenkins.primary_event is None or not use_llm:
        return render_jenkins_human(report)
    try:
        narrative = interpret_events(
            path,
            jenkins.primary_event,
            model,
            prompt=JENKINS_REPORT_PROMPT,
            incident_text=format_jenkins_model_input(jenkins),
            base_url=base_url,
            timeout=timeout,
        )
    except GenerationError:
        print(
            "The model did not produce a summary. Using the extracted events.",
            file=sys.stderr,
        )
        narrative = None
    return render_jenkins_human(report, narrative)
