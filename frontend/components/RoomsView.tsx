"use client";

import { type DragEvent, useMemo, useState } from "react";
import {
  assignmentMatches,
  dayAxis,
  formatDayLabel,
  formatTime,
  panelDayLetter,
  roomSummaries,
  type RoomSummary,
  type SlotAxis,
  slotAxis,
} from "@/lib/schedule";
import type { Assignment, RoomPanel } from "@/lib/types";
import { EmptyState, SearchBar } from "./ui";

/** Payload MIME for a panel badge dragged between room cards. A private type
 * so the card's drop zone ignores any other drag (text selections, files). */
const PANEL_DND_MIME = "application/x-meridian-panel";

/** One card per room, for a single day at a time (FR-32).
 *
 * The day selector mirrors RoomView's: prev/next arrows around a date label,
 * and only the days this run actually uses appear. For the chosen day every
 * room active that day gets a card carrying its run-wide summary (interview
 * count, panels, divisions represented) and, below it, that room's own
 * slot-by-slot schedule — the same time -> applicant -> division rows
 * RoomView shows, but scoped to the one room instead of spread across every
 * panel side by side.
 *
 * The "divisions represented" chips double as a filter: exactly one is active
 * at a time and the schedule below shows only that division's interviews, so a
 * room card reads as one division's running order rather than every division
 * interleaved. The selection is per-card — each room keeps its own. When the
 * active division runs on more than one panel in the same room, the schedule
 * gains a column per panel so the two running orders never collide in a cell.
 *
 * Clicking an interview calls `onSelect`, the same hook RoomView uses to open
 * the move / detail dialog.
 */
export function RoomsView({
  assignments,
  panels = [],
  divisions = [],
  onSelect,
  onAddPanel,
  onDeletePanel,
  onMovePanel,
}: {
  assignments: Assignment[];
  /** Every panel in the run (solver + manually added), for the per-room panel
   * badges and the "add panel" dropdown's disabled state. */
  panels?: RoomPanel[];
  /** All division codes — the "+" dropdown lists these. */
  divisions?: string[];
  onSelect: (a: Assignment) => void;
  onAddPanel?: (division: string, room: string) => void;
  onDeletePanel?: (panelId: string) => void;
  /** Drag a panel badge from one room card onto another to relocate the whole
   * panel (and its interviews) to that room. */
  onMovePanel?: (panelId: string, room: string) => void;
}) {
  const days = useMemo(() => dayAxis(assignments), [assignments]);
  const [rawDay, setRawDay] = useState(0);
  const [query, setQuery] = useState("");

  if (days.length === 0) {
    return <EmptyState title="This run has no assignments to show." />;
  }

  const dayIndex = Math.min(rawDay, days.length - 1);
  const date = days[dayIndex];
  const onDay = assignments.filter((a) => a.date === date);
  // "CREATIVE-A1" is Thursday, "CREATIVE-B1" is Friday (config/panels.yaml's
  // `[DIVISION]-[DAY][N]` convention) — the Nth day selected here is day
  // letter N (A, B, ...), independent of the actual calendar dates.
  const dayLetter = String.fromCharCode(65 + dayIndex);

  /** This day's summary per room — interview count, panels and divisions
   * scoped to `onDay` so switching days never mixes Thursday's "A" panels
   * with Friday's "B" ones in the same card. */
  const summaryByRoom = new Map<string, RoomSummary>();
  for (const s of roomSummaries(onDay)) summaryByRoom.set(s.room, s);
  // Rooms with an interview today, plus any room that now holds only a
  // manually-added (possibly empty) panel — so a room a recruiter just added a
  // panel to still gets a card to drag into.
  const rooms = [
    ...new Set([
      ...onDay.map((a) => a.room),
      ...panels.filter((p) => p.manual).map((p) => p.room),
    ]),
  ].sort((x, y) => x.localeCompare(y, undefined, { numeric: true }));

  /** The full slot axis for the chosen day, spanning every room. Each room
   * card renders one row per entry here regardless of which division is
   * selected, so rows stay aligned across cards and never "squish" when a
   * division fills only part of the day. */
  const daySlots = slotAxis(onDay);

  return (
    <div>
      <SearchBar value={query} onChange={setQuery} />
      <div className="mb-3 flex items-center gap-3">
        <button
          type="button"
          onClick={() => setRawDay(dayIndex - 1)}
          disabled={dayIndex === 0}
          aria-label="Previous day"
          className="rounded-md border border-neutral-300 bg-white px-2.5 py-1 text-sm text-neutral-700 transition-colors hover:bg-neutral-50 disabled:cursor-not-allowed disabled:opacity-35"
        >
          ‹
        </button>
        <span className="min-w-[10rem] text-center text-sm font-semibold text-neutral-900">
          {formatDayLabel(date)}
        </span>
        <button
          type="button"
          onClick={() => setRawDay(dayIndex + 1)}
          disabled={dayIndex >= days.length - 1}
          aria-label="Next day"
          className="rounded-md border border-neutral-300 bg-white px-2.5 py-1 text-sm text-neutral-700 transition-colors hover:bg-neutral-50 disabled:cursor-not-allowed disabled:opacity-35"
        >
          ›
        </button>
        <span className="text-xs text-neutral-500">
          Day {dayIndex + 1} of {days.length}
        </span>
      </div>

      {rooms.length === 0 ? (
        <EmptyState title="Nothing is scheduled on this day." />
      ) : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {rooms.map((room) => (
            <RoomCard
              key={room}
              room={room}
              summary={summaryByRoom.get(room)}
              assignments={onDay.filter((a) => a.room === room)}
              roomPanels={panels.filter((p) => p.room === room)}
              dayLetter={dayLetter}
              allDivisions={divisions}
              slots={daySlots}
              query={query}
              onSelect={onSelect}
              onAddPanel={onAddPanel}
              onDeletePanel={onDeletePanel}
              onMovePanel={onMovePanel}
            />
          ))}
        </div>
      )}
    </div>
  );
}

/** A single room's card: the run-wide summary header, a one-of division
 * selector, then this day's schedule for the selected division as a vertical
 * time -> applicant list. If that division runs on two panels in this one
 * room, the schedule splits into a column per panel (labelled with the panel
 * id) so their running orders sit side by side instead of piling into one
 * cell. */
function RoomCard({
  room,
  summary,
  assignments,
  roomPanels,
  dayLetter,
  allDivisions,
  slots,
  query,
  onSelect,
  onAddPanel,
  onDeletePanel,
  onMovePanel,
}: {
  room: string;
  summary: RoomSummary | undefined;
  assignments: Assignment[];
  /** Every panel this run has in this room (solver + manually added), both
   * event days. Used as-is for room-exclusivity (the "+" menu greys out a
   * division already running here on either day, matching the backend); the
   * header badges filter this down to `dayLetter` below. */
  roomPanels: RoomPanel[];
  /** The currently selected day's letter ("A" or "B") — panel badges and
   * everything derived from them are scoped to this. */
  dayLetter: string;
  /** All division codes, for the "add panel" dropdown. */
  allDivisions: string[];
  /** The whole day's slot axis, shared by every card. Rows are drawn for all
   * of these, not just the slots the active division happens to fill. */
  slots: SlotAxis[];
  query: string;
  onSelect: (a: Assignment) => void;
  onAddPanel?: (division: string, room: string) => void;
  onDeletePanel?: (panelId: string) => void;
  onMovePanel?: (panelId: string, room: string) => void;
}) {
  // A panel badge from another room card is hovering over this one.
  const [dragOver, setDragOver] = useState(false);
  const interviewCount = summary?.interviewCount ?? assignments.length;
  const clashes = summary?.clashes ?? 0;

  /** Panel badges for the header, scoped to the selected day (FR-32 bugfix —
   * a room's card must show only "-A" panels on Thursday, only "-B" on
   * Friday, never both at once). Prefer the run's real panel list (carries
   * `manual` / `deletable`); fall back to the ids seen in assignments/summary
   * when the panel fetch has not landed — both are already day-scoped by the
   * caller (`roomPanels` filtered here, `summary`/`assignments` built from
   * that day's assignments only). */
  const badgePanels: RoomPanel[] = useMemo(() => {
    const onThisDay = roomPanels.filter(
      (p) => panelDayLetter(p.panel_id) === dayLetter,
    );
    if (onThisDay.length > 0) {
      return [...onThisDay].sort((x, y) =>
        x.panel_id.localeCompare(y.panel_id, undefined, { numeric: true }),
      );
    }
    const ids =
      summary?.panels ?? [...new Set(assignments.map((a) => a.panel_id))];
    return ids
      .filter((id) => panelDayLetter(id) === dayLetter)
      .map((id) => ({
        panel_id: id,
        division: "",
        room,
        interview_count: 1,
        manual: false,
        deletable: false,
      }));
  }, [roomPanels, dayLetter, summary, assignments, room]);

  /** Chips come from this day's summary (most interviews first) so a room's
   * "divisions represented" reflects only the day currently selected; fall
   * back to this day's own assignments if there is no summary. */
  const divisions = useMemo(() => {
    if (summary?.divisions) return summary.divisions;
    const counts = new Map<string, number>();
    for (const a of assignments) {
      counts.set(a.division, (counts.get(a.division) ?? 0) + 1);
    }
    return [...counts.entries()]
      .map(([division, count]) => ({ division, count }))
      .sort(
        (x, y) => y.count - x.count || x.division.localeCompare(y.division),
      );
  }, [summary, assignments]);

  // Default to the first (largest) division; `?? divisions[0]` keeps a sensible
  // active chip even before the user has clicked or if data arrives late.
  const [picked, setPicked] = useState<string | null>(null);
  const activeDivision = picked ?? divisions[0]?.division ?? null;

  const shown = useMemo(
    () =>
      activeDivision
        ? assignments.filter((a) => a.division === activeDivision)
        : assignments,
    [assignments, activeDivision],
  );

  const bySlot = useMemo(() => {
    const map = new Map<string, Assignment[]>();
    for (const a of shown) {
      const bucket = map.get(a.slot_id);
      if (bucket) bucket.push(a);
      else map.set(a.slot_id, [a]);
    }
    return map;
  }, [shown]);

  /** The panels the active division runs in this room. A division can be split
   * across two panels sharing one room (e.g. MEDMARDOC-A1 and MEDMARDOC-A2
   * both in Room 2018); when that happens each gets its own column so their
   * running orders never overlap in a single cell. */
  const divisionPanels = useMemo(
    () =>
      [...new Set(shown.map((a) => a.panel_id))].sort((x, y) =>
        x.localeCompare(y, undefined, { numeric: true }),
      ),
    [shown],
  );
  const multiPanel = divisionPanels.length > 1;

  /** Accept a panel badge dropped from another room card. */
  const dropProps = onMovePanel
    ? {
        onDragOver: (e: DragEvent) => {
          if (!e.dataTransfer.types.includes(PANEL_DND_MIME)) return;
          e.preventDefault();
          e.dataTransfer.dropEffect = "move";
          setDragOver(true);
        },
        onDragLeave: () => setDragOver(false),
        onDrop: (e: DragEvent) => {
          setDragOver(false);
          const raw = e.dataTransfer.getData(PANEL_DND_MIME);
          if (!raw) return;
          e.preventDefault();
          let payload: { panelId: string; fromRoom: string };
          try {
            payload = JSON.parse(raw);
          } catch {
            return;
          }
          if (payload.fromRoom === room) return;
          onMovePanel(payload.panelId, room);
        },
      }
    : {};

  return (
    <section
      {...dropProps}
      className={`overflow-hidden rounded-lg border bg-white ${
        dragOver
          ? "border-sky-400 ring-2 ring-inset ring-sky-300"
          : "border-neutral-200"
      }`}
    >
      <header className="flex items-start justify-between border-b border-neutral-200 px-4 py-2.5">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold text-neutral-900">
            Room {room}
          </h3>
          <div className="mt-1 flex flex-wrap items-center gap-1">
            {badgePanels.length === 0 ? (
              <span className="text-xs text-neutral-400">no panels</span>
            ) : (
              badgePanels.map((p) => (
                <span
                  key={p.panel_id}
                  draggable={Boolean(onMovePanel)}
                  onDragStart={
                    onMovePanel
                      ? (e: DragEvent) => {
                          e.dataTransfer.effectAllowed = "move";
                          e.dataTransfer.setData(
                            PANEL_DND_MIME,
                            JSON.stringify({
                              panelId: p.panel_id,
                              fromRoom: room,
                            }),
                          );
                        }
                      : undefined
                  }
                  title={
                    onMovePanel
                      ? `Drag onto another room card to move ${p.panel_id} there`
                      : p.manual && !p.deletable
                        ? `${p.interview_count} interview(s) — move them out before this panel can be deleted`
                        : p.manual
                          ? "Manually added — empty, safe to delete"
                          : undefined
                  }
                  className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[11px] font-medium ${
                    onMovePanel ? "cursor-grab active:cursor-grabbing" : ""
                  } ${
                    p.manual
                      ? "border-sky-200 bg-sky-50 text-sky-700"
                      : "border-neutral-200 bg-neutral-100 text-neutral-600"
                  }`}
                >
                  {p.panel_id}
                  {p.deletable && onDeletePanel && (
                    <button
                      type="button"
                      onClick={() => onDeletePanel(p.panel_id)}
                      aria-label={`Delete empty panel ${p.panel_id}`}
                      className="-mr-0.5 leading-none text-sky-400 transition-colors hover:text-red-600"
                    >
                      ×
                    </button>
                  )}
                </span>
              ))
            )}
            {onAddPanel && allDivisions.length > 0 && (
              <AddPanelMenu
                divisions={allDivisions}
                usedDivisions={new Set(roomPanels.map((p) => p.division))}
                onAdd={(division) => onAddPanel(division, room)}
              />
            )}
          </div>
        </div>
        <p className="shrink-0 pl-2 text-xs text-neutral-500">
          {interviewCount} interview{interviewCount === 1 ? "" : "s"}
          {clashes > 0 && (
            <span className="ml-1 text-red-600">· {clashes} clash</span>
          )}
        </p>
      </header>

      {divisions.length > 0 && (
        <div className="border-b border-neutral-100 px-4 py-3">
          <p className="mb-2 text-xs uppercase tracking-wide text-neutral-400">
            Divisions represented ({divisions.length}) — pick one
          </p>
          <ul className="flex flex-wrap gap-1.5" role="tablist">
            {divisions.map((d) => {
              const active = d.division === activeDivision;
              return (
                <li key={d.division}>
                  <button
                    type="button"
                    role="tab"
                    aria-selected={active}
                    onClick={() => setPicked(d.division)}
                    className={`inline-flex items-center rounded border px-2 py-0.5 text-xs font-medium transition-colors ${
                      active
                        ? "border-neutral-800 bg-neutral-800 text-white"
                        : "border-neutral-200 bg-neutral-100 text-neutral-600 hover:bg-neutral-200"
                    }`}
                  >
                    {d.division}
                    <span
                      className={`ml-1 tabular-nums ${
                        active ? "text-neutral-300" : "text-neutral-400"
                      }`}
                    >
                      {d.count}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        </div>
      )}

      {slots.length === 0 ? (
        <p className="px-4 py-3 text-xs text-neutral-400">
          Nothing scheduled in this room today.
        </p>
      ) : multiPanel ? (
        <div className="overflow-x-auto">
          <table className="min-w-full border-collapse text-xs">
            <thead>
              <tr className="border-b border-neutral-200">
                <th className="w-24 border-r border-neutral-100 bg-neutral-50 px-3 py-2 text-left font-medium uppercase tracking-wide text-neutral-400">
                  Slot
                </th>
                {divisionPanels.map((panelId) => (
                  <th
                    key={panelId}
                    className="min-w-[9rem] border-r border-neutral-100 bg-neutral-50 px-3 py-2 text-left font-medium text-neutral-700 last:border-r-0"
                  >
                    {panelId}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {slots.map((slot) => {
                const here = bySlot.get(slot.slot_id) ?? [];
                return (
                  <tr
                    key={slot.slot_id}
                    className="border-b border-neutral-100 last:border-0"
                  >
                    <SlotTimeHeader slot={slot} />
                    {divisionPanels.map((panelId) => (
                      <td
                        key={panelId}
                        className="border-r border-neutral-100 p-0 align-top last:border-r-0"
                      >
                        <ScheduleCell
                          items={here.filter((a) => a.panel_id === panelId)}
                          query={query}
                          onSelect={onSelect}
                        />
                      </td>
                    ))}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <table className="min-w-full border-collapse text-xs">
          <tbody>
            {slots.map((slot) => {
              const here = bySlot.get(slot.slot_id) ?? [];
              return (
                <tr
                  key={slot.slot_id}
                  className="border-b border-neutral-100 last:border-0"
                >
                  <SlotTimeHeader slot={slot} />
                  <td className="p-0 align-top">
                    <ScheduleCell
                      items={here}
                      query={query}
                      onSelect={onSelect}
                    />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </section>
  );
}

/** The two-line "start over end" row header, identical in the single- and
 * multi-panel layouts so every time cell is the same height. */
function SlotTimeHeader({ slot }: { slot: SlotAxis }) {
  return (
    <th
      scope="row"
      className="w-24 border-r border-neutral-100 bg-neutral-50 px-3 py-2 text-left align-top font-normal"
    >
      <span className="block font-medium tabular-nums text-neutral-700">
        {formatTime(slot.start_time)}
      </span>
      <span className="block tabular-nums text-neutral-400">
        {formatTime(slot.end_time)}
      </span>
    </th>
  );
}

/** One slot's worth of interviews for a single column: the blank marker when
 * empty, otherwise one clickable row per applicant. Shared by both layouts. */
function ScheduleCell({
  items,
  query,
  onSelect,
}: {
  items: Assignment[];
  query: string;
  onSelect: (a: Assignment) => void;
}) {
  if (items.length === 0) {
    return <div className="min-h-[2.75rem] px-3 py-2 text-neutral-300">·</div>;
  }
  return (
    <>
      {items.map((a) => (
        <button
          key={a.assignment_id}
          type="button"
          onClick={() => onSelect(a)}
          title={buildTitle(a)}
          className={`block min-h-[2.75rem] w-full px-3 py-2 text-left transition-colors ${
            a.is_clash
              ? "bg-red-100 text-red-600 hover:bg-red-200"
              : "text-neutral-800 hover:bg-neutral-100"
          } ${
            assignmentMatches(a, query)
              ? "ring-2 ring-inset ring-amber-500"
              : ""
          }`}
        >
          <span className="block truncate pr-4 font-medium">{a.full_name}</span>
          <span
            className={`block truncate ${a.is_clash ? "text-red-500" : "text-neutral-500"}`}
          >
            {a.sub_division}
          </span>
        </button>
      ))}
    </>
  );
}

/** The "+" on a room card. Opens a dropdown of every division; one already
 * running a panel in this room is greyed out and unselectable, since a room
 * cannot host two panels of the same division (room-exclusivity). Picking an
 * available division creates a new empty panel for it in this room. */
function AddPanelMenu({
  divisions,
  usedDivisions,
  onAdd,
}: {
  divisions: string[];
  usedDivisions: Set<string>;
  onAdd: (division: string) => void;
}) {
  const [open, setOpen] = useState(false);

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="Add a panel to this room"
        className="inline-flex h-[22px] w-[22px] items-center justify-center rounded border border-dashed border-neutral-300 text-sm leading-none text-neutral-500 transition-colors hover:border-neutral-400 hover:bg-neutral-50 hover:text-neutral-800"
      >
        +
      </button>
      {open && (
        <>
          {/* click-away layer */}
          <button
            type="button"
            aria-hidden
            tabIndex={-1}
            onClick={() => setOpen(false)}
            className="fixed inset-0 z-10 cursor-default"
          />
          <ul
            role="menu"
            className="absolute left-0 z-20 mt-1 max-h-64 w-44 overflow-y-auto rounded-md border border-neutral-200 bg-white py-1 shadow-lg"
          >
            {divisions.map((d) => {
              const used = usedDivisions.has(d);
              return (
                <li key={d} role="none">
                  <button
                    type="button"
                    role="menuitem"
                    disabled={used}
                    onClick={() => {
                      onAdd(d);
                      setOpen(false);
                    }}
                    title={
                      used
                        ? "This division already has a panel in this room"
                        : undefined
                    }
                    className={`block w-full px-3 py-1.5 text-left text-xs ${
                      used
                        ? "cursor-not-allowed text-neutral-300"
                        : "text-neutral-700 hover:bg-neutral-100"
                    }`}
                  >
                    {d}
                  </button>
                </li>
              );
            })}
          </ul>
        </>
      )}
    </div>
  );
}

function buildTitle(a: Assignment): string {
  return [
    a.full_name,
    a.sub_division,
    a.is_clash ? "clash" : null,
    a.is_locked ? "locked" : null,
  ]
    .filter(Boolean)
    .join(" · ");
}
