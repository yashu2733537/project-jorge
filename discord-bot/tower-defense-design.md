# Tiny Tower Defense — Design Document

A minimal, playable tower defense prototype. Scope is intentionally small: one map, a handful of towers and enemies, and ~10 waves. Goal is a vertical slice you can finish (win or lose) in under 10 minutes.

## 1. Core Mechanics

- **Grid-based field**: Enemies move along a fixed path from spawn to the base. Towers are placed on buildable grid cells.
- **Resources**: One currency — **Gold**. Earned by killing enemies. Spent on building and upgrading towers.
- **Build/Upgrade**: Click a buildable cell to place a tower. Click an existing tower to upgrade (max level 3) or sell (refund 50%).
- **Wave system**: Waves spawn automatically; the player presses "Start Wave" to begin the next one. Between waves, the player can build freely.
- **Lives**: The base has N lives (e.g., 20). Each enemy that reaches the base costs 1 life. At 0 lives, game over.
- **Win condition**: Survive all waves. **Lose condition**: Lose all lives.
- **Speed controls**: Pause, 1x, 2x to keep it snappy and testable.

### Interaction flow (prototype)
1. Game starts at Wave 0 — build phase.
2. Player presses "Start Wave 1".
3. Enemies spawn and walk the path; towers auto-attack.
4. Wave ends → reward bonus gold → back to build phase.
5. Repeat until all waves cleared or lives run out.

## 2. Tower Types

Keep it to **3 towers**, each with a distinct role (single-target, AoE, support). 3 upgrade levels per tower.

| Tower | Role | Damage style | Level scaling | Notes |
|---|---|---|---|---|
| **Cannon** | Single-target DPS | Fast, low damage per shot | Damage +15%, Fire rate +10% per level | Cheap, reliable starter |
| **Frost** | Slow / crowd control | Low damage, applies **slow** (e.g., -40% move speed for 2s) | Slow % + slow duration up | Crucial vs fast enemies; no damage scaling |
| **Tesla** | AoE | Medium damage to **all** enemies in range | Radius + damage up | Effective vs clumped groups |

Balance baseline (level 1): range 2 cells, 0.5–1.0s fire rate, damage tuned so ~2 cannon shots kill a basic enemy.

## 3. Enemy Types

**3 enemy types**, introduced gradually across waves. All follow the same path.

| Enemy | HP | Speed | Reward | Behavior |
|---|---|---|---|---|
| **Runner** | low | 1.0x | low | Basic; appears from wave 1 |
| **Brute** | high | 0.6x | high | Tank; appears from wave 4 |
| **Swift** | low | 1.8x | medium | Fast; weak to Frost slow; appears from wave 7 |

Optional (stretch): a **Healer** every 10th wave that regenerates nearby enemies — cut if it adds scope creep.

## 4. Wave System

- **10 waves** total. Each wave defined by a spawn table: `[(enemy_type, count), ...]` and a spawn interval.
- Wave `n` difficulty scales linearly: enemy HP × (1 + 0.15 × n), spawn interval shrinks slightly.
- Composition ramps: Waves 1–3 runners only → 4–6 add brutes → 7–10 mix all three.
- **Wave bonus**: on clearing a wave, grant a gold bonus so the player can always afford something new next round.
- Wave scheduling is data-driven (JSON table), not hardcoded logic — easy to retune.

## 5. Map / Grid Design

- **Grid**: 12 × 8 cells (96 cells). Small enough to reason about, big enough for a real path.
- **Cell types**: `path`, `buildable`, `base`, `spawn` (ASCII tile map).
- **Path**: fixed, winding S-curve from spawn (left edge) to base (right edge), making the far edge of curves valuable for towers. Precomputed waypoint list for enemy movement.
- **Buildable cells**: all non-path, non-base cells. No terrain blockers in the prototype.
- Movement: enemies lerp between path waypoints; towers use range in cells (Chebyshev or Euclidean, pick one — recommend Euclidean with center-of-cell).

Example tile layout (8 rows shown as 12 chars):

```
S#######D####   S=spawn, D=base
#........#...
#........#...
#.#########...
#....#........
####.#...####.
....#.#....#..
....D.........
```

## 6. Progression

- **Within a run**: gold income → tower buying/upgrading → survive harder waves. The only progression in the prototype.
- **Meta progression (out of scope for v1)**: no persistent unlocks, no save system. Optional future: unlockable tower skins/start gold based on best wave reached.

## 7. Tech Stack Recommendations

Recommendation for a **minimal playable prototype**:

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **Python + Pygame** | Fast to write, readable logic, zero build step, easy math/tuning, great for grid games | Not web-shareable, packaging friction | ⭐ **Best fit for a quick solo prototype** |
| **Web / JS (Canvas + TS)** | Shareable, no install for players, good if you later want to ship in browser | More boilerplate (loop, input, state), tooling setup | Strong second choice |
| **Godot** | Full editor, scene system, physics | Heavier setup, steeper for pure-logic dev | Good if you plan to grow past prototype |
| **Unity** | Most powerful, asset ecosystem | Overkill for this scope, slow iteration | Skip for prototype |

### Recommended stack (Python/Pygame)
- Python 3.11+, `pygame` only (no other deps).
- Modules:
  - `main.py` — game loop, clock, state machine.
  - `grid.py` — tile map, waypoints, pathfinding-free movement (fixed path).
  - `towers.py` / `enemies.py` — data + update/render.
  - `waves.py` — wave scheduler from a JSON table.
  - `ui.py` — buttons (Start Wave, Speed), gold/lives HUD, tower menu.
- Visuals: solid-color rectangles/circles for towers and enemies (no assets needed).
- Target: ~800–1,200 LOC, playable end-to-end in a single sitting.

## 8. Milestones

1. **M1 — Skeleton**: window, grid render, fixed path, dummy enemy walks to base. (Half a day)
2. **M2 — Towers**: place/upgrade/sell, 3 towers with shooting + projectiles. (Half a day)
3. **M3 — Waves**: JSON wave table, spawner, win/lose, gold/lives economy. (Half a day)
4. **M4 — Polish**: pause/2x speed, balancing pass, simple sounds (optional). (A few hours)

Total estimate: **2–3 focused days** to a finished prototype.

## 9. Out of Scope (v1)

- Multiple maps, map editor, abilities/spells, hero units, meta progression, online/saves, audio beyond optional SFX, animation beyond simple movement.
