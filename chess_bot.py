import json
import os
import re
import shutil
from pathlib import Path

import chess
import chess.engine

STOCKFISH_CANDIDATES = [
    os.environ.get("STOCKFISH"),
    str(Path.home() / ".local/bin/stockfish"),
    shutil.which("stockfish"),
    "/usr/games/stockfish",
]


def _spawn_engine() -> chess.engine.SimpleEngine:
    path = next((p for p in STOCKFISH_CANDIDATES if p and Path(p).exists()), None)
    if path is None:
        raise FileNotFoundError(
            "Stockfish not found — install it (STOCKFISH env var or ~/.local/bin/stockfish)."
        )
    return chess.engine.SimpleEngine.popen_uci(path)


def _looks_like_fen(text: str) -> bool:
    return "/" in text or text.count(" ") >= 5


def _normalize_san(san: str) -> str:
    san = san.strip()
    if san.lower() in ("o-o", "0-0"):
        return "O-O"
    if san.lower() in ("o-o-o", "0-0-0"):
        return "O-O-O"
    if san and san[0] in "kqrbnKQRBN":
        return san[0].upper() + san[1:]
    return san


def _parse_position(text: str) -> tuple[chess.Board, str]:
    text = (text or "").strip()
    if not text or text.lower() in ("start", "startpos", "new", "default", "begin"):
        return chess.Board(), "start position"
    if _looks_like_fen(text):
        try:
            board = chess.Board(text)
        except ValueError:
            raise ValueError(f"not a valid FEN: {text!r}")
        return board, f"fen: {text}"
    low = text.lower()
    tokens = [m for m in re.split(r"[.\s,]+", low) if m]
    board = chess.Board()
    for tok in tokens:
        if tok.isdigit():
            continue
        san = _normalize_san(tok)
        try:
            board.push_san(san)
        except ValueError:
            uci = re.fullmatch(r"[a-h][1-8][a-h][1-8][qrbn]?", tok)
            if uci:
                try:
                    board.push_uci(tok)
                    continue
                except ValueError:
                    pass
            raise ValueError(f"illegal move: {tok!r} in {text!r}")
    return board, "moves: " + " ".join(tokens)


def _format_eval(score: chess.engine.PovScore) -> str:
    s = score.pov(chess.WHITE)
    if s.is_mate():
        return f"mate in {abs(s.mate())}"
    cp = s.score()
    sign = "+" if cp > 0 else ""
    return f"{sign}{cp / 100:.1f}"


_UNI = {
    "P": "♙", "N": "♘", "B": "♗", "R": "♖", "Q": "♕", "K": "♔",
    "p": "♟", "n": "♞", "b": "♝", "r": "♜", "q": "♛", "k": "♚", ".": "·",
}


def _render(board: chess.Board, perspective: str | None = None) -> str:
    rows = str(board).split("\n")
    files = "a b c d e f g h"
    if perspective is None:
        perspective = "black" if board.turn == chess.BLACK else "white"
    if perspective == "black":
        rows = [r[::-1] for r in reversed(rows)]
        files = files[::-1]
        ranks = range(1, 9)
    else:
        ranks = range(8, 0, -1)
    lines = ["  " + files]
    for row, n in zip(rows, ranks):
        cells = " ".join(_UNI.get(ch, ch) for ch in row)
        lines.append(f"{n} {cells} {n}")
    lines.append("  " + files)
    return "```\n" + "\n".join(lines) + "\n```"


def _push_tok(board: chess.Board, tok: str) -> None:
    san = _normalize_san(tok)
    try:
        board.push_san(san)
        return
    except ValueError:
        uci = re.fullmatch(r"[a-h][1-8][a-h][1-8][qrbn]?", tok)
        if uci:
            try:
                board.push_uci(tok)
                return
            except ValueError:
                pass
    raise ValueError(f"illegal move: {tok!r}")


# ---------------- vs mode ----------------

BASE_DIR = Path(__file__).resolve().parent
GAMES_FILE = BASE_DIR / "chess_games.json"
CHALLENGES_FILE = BASE_DIR / "chess_challenges.json"

ELO_MIN = 1320
ELO_MAX = 3190


def _load_games() -> dict:
    try:
        return json.loads(GAMES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_games(games: dict) -> None:
    try:
        GAMES_FILE.write_text(json.dumps(games), encoding="utf-8")
    except OSError:
        pass


def _game_status(board: chess.Board) -> str | None:
    if board.is_checkmate():
        winner = "you" if board.turn == chess.BLACK else "jorge"
        return f"**Checkmate** — {winner} win! 🎉"
    if board.is_stalemate():
        return "**Draw** — stalemate. 🤝"
    if board.is_insufficient_material():
        return "**Draw** — insufficient material. 🤝"
    if board.is_repetition(3) or board.is_fifty_moves():
        return "**Draw** — repetition/fifty-move rule. 🤝"
    return None


def has_game(user: str) -> bool:
    return user in _load_games()


def _find_game(games: dict, user: str) -> dict | None:
    game = games.get(user)
    if game is not None:
        return game
    for g in games.values():
        if g.get("pvp") and user in (g.get("white"), g.get("black")):
            return g
    return None


def _load_challenges() -> dict:
    try:
        return json.loads(CHALLENGES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_challenges(ch: dict) -> None:
    try:
        CHALLENGES_FILE.write_text(json.dumps(ch), encoding="utf-8")
    except OSError:
        pass


def chess_challenge(challenger: str, query: str) -> str:
    parts = (query or "").split()
    if not parts:
        return "♟ challenge failed — I need to know who to challenge. Try `@jorge chess vs @user`."
    target = parts[0]
    if target == challenger:
        return "♟ you can't challenge yourself 😅"
    ch = _load_challenges()
    ch[target] = {"challenger": challenger, "challenger_name": " ".join(parts[1:]) or "someone"}
    _save_challenges(ch)
    name = ch[target]["challenger_name"]
    return f"⚔️ <@{target}>, **{name}** challenges you to chess! Reply `!accept` to play or `!decline` to refuse."


def chess_accept(accepter: str) -> str:
    ch = _load_challenges()
    entry = ch.pop(accepter, None)
    if entry is None:
        return "♟ no pending challenge for you — someone has to `@jorge chess vs @you` first."
    _save_challenges(ch)
    challenger = entry["challenger"]
    games = _load_games()
    games[challenger] = {
        "fen": chess.Board().fen(),
        "pvp": True,
        "white": challenger,
        "black": accepter,
        "white_name": entry["challenger_name"],
    }
    _save_games(games)
    return (
        "⚔️ **Game on!** <@"
        + challenger
        + "> (white) vs <@"
        + accepter
        + "> (black). Players move with `!move <san>`.\n\n"
        + _render(chess.Board())
    )


def chess_decline(accepter: str) -> str:
    ch = _load_challenges()
    entry = ch.pop(accepter, None)
    if entry is None:
        return "♟ no pending challenge for you."
    _save_challenges(ch)
    return f"♟ <@{accepter}> declined {entry['challenger_name']}'s challenge. No game."


def new_game(user: str, elo: int, side: str = "white") -> str:
    elo = max(ELO_MIN, min(ELO_MAX, int(elo)))
    games = _load_games()
    board = chess.Board()
    games[user] = {"fen": board.fen(), "elo": elo, "user_side": side}
    _save_games(games)
    msg = (
        f"♟ **jorge vs you** — new game started! (jorge rated **{elo}**)\n"
        + f"You play **{side}**. Make your move with `!move <san>` (e.g. `!move e4`).\n"
        + f"Resign anytime with `!move resign`.\n\n"
        + _render(board, perspective=side)
    )
    if side != "white":
        msg += "\n\njorge moves first…\n" + _jorge_move(user, board, games)
    return msg


def _jorge_move(user: str, board: chess.Board, games: dict) -> str:
    elo = games.get(user, {}).get("elo", ELO_MIN)
    engine = _spawn_engine()
    try:
        engine.configure({"UCI_LimitStrength": True, "UCI_Elo": elo})
        info = engine.analyse(board, chess.engine.Limit(time=1.5))
        jorge_move = info["pv"][0]
        san = board.san(jorge_move)
        board.push(jorge_move)
    except Exception as e:
        return f"♟ jorge failed to move: {e}"
    finally:
        try:
            engine.quit()
        except Exception:
            pass
    games[user]["fen"] = board.fen()
    _save_games(games)
    side = games.get(user, {}).get("user_side", "white")
    return f"**jorge** plays `{san}`\n" + _render(board, perspective=side)


def play_move(user: str, move: str) -> str:
    move = (move or "").strip()
    games = _load_games()
    game = _find_game(games, user)
    if game is None:
        return "♟ no game in progress — start one with `!chess-vs <elo>` or get challenged with `@jorge chess vs @someone`."
    if move.lower() in ("resign", "quit", "gg", "surrender"):
        del games[game.get("white", user)]
        _save_games(games)
        winner = "jorge"
        if game.get("pvp"):
            winner = "the other player" if user == game.get("white") else f"<@{game.get('white')}>"
        return f"🤝 You resigned — **{winner} wins**. New game anytime!"
    board = chess.Board(game["fen"])
    if move.lower() in ("start", "board", "fen", "show"):
        return _render(board, perspective=game.get("user_side") if not game.get("pvp") else None)
    if game.get("pvp"):
        my_side = game.get("white") == user
        if (board.turn == chess.WHITE) != my_side:
            opp = "<@%s>" % (game.get("black") if my_side else game.get("white"))
            return f"♟ Not your turn — {opp} is thinking. Wait for them to move."
        if user not in (game.get("white"), game.get("black")):
            return "♟ You're not a player in this game."
    try:
        _push_tok(board, move)
    except ValueError as e:
        return f"♟ {e}"
    status = _game_status(board)
    if status:
        del games[game.get("white", user)]
        _save_games(games)
        return f"Your move `{move}` → {status}"
    games[game.get("white", user)]["fen"] = board.fen()
    _save_games(games)
    if game.get("pvp"):
        opp = "<@%s>" % (game.get("black") if game.get("white") == user else game.get("white"))
        return f"You played `{move}`\n\n" + _render(board) + f"\n\nWaiting on {opp}…"
    return f"You played `{move}`\n\n" + _jorge_move(user, board, games) + _maybe_end(user, board, games)


def _maybe_end(user: str, board: chess.Board, games: dict) -> str:
    status = _game_status(board)
    if status:
        del games[user]
        _save_games(games)
        return "\n\n" + status
    return ""


def analyze(text: str) -> str:
    try:
        board, label = _parse_position(text)
    except ValueError as e:
        return f"♟ {e}"
    try:
        engine = _spawn_engine()
    except FileNotFoundError as e:
        return f"♟ {e}"

    try:
        if board.is_checkmate():
            winner = "white" if board.turn == chess.BLACK else "black"
            return f"♟ **Checkmate** — {label}. Winner: {winner}."
        if board.is_stalemate() or board.is_insufficient_material() or board.is_repetition(3):
            return f"♟ **Draw** — {label}. (stalemate/insufficient material/repetition)"

        lines: list[str] = []
        try:
            info = engine.analyse(
                board,
                chess.engine.Limit(depth=16, time=2.5),
                multipv=3,
            )
        except Exception as e:
            return f"♟ analysis failed: {e}"
        for i, line in enumerate(info, 1):
            move = line["pv"][0]
            san = board.san(move)
            board.push(move)
            pv_sans = " ".join(board.variation_san(line["pv"][1:])[:6].split())
            board.pop()
            ev = _format_eval(line["score"])
            lines.append(f"{i}. {san}  {ev}" + (f"  ({pv_sans})" if pv_sans else ""))

        side = "White" if board.turn == chess.WHITE else "Black"
        board_txt = _render(board)
        return (
            f"♟ {side} to move · {label} · stockfish\n"
            + board_txt
            + "\n\nBest moves:\n"
            + "\n".join(lines)
        )
    finally:
        try:
            engine.quit()
        except Exception:
            pass