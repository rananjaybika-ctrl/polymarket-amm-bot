import sys
import json
import datetime
import requests
import matplotlib.pyplot as plt
import numpy as np
import matplotlib.patheffects as pe
import matplotlib.ticker as ticker

# ---------------------------------------------------
# CONFIG
# ---------------------------------------------------
STYLES = {
    ("Buy", "Up"):   ("#008f00", "x", "Buy YES"),   # strong green
    ("Sell", "Up"):  ("#00c800", "o", "Sell YES"),  # vivid lime
    ("Buy", "Down"): ("#d000d0", "x", "Buy NO"),    # strong magenta
    ("Sell", "Down"):("#d40000", "o", "Sell NO")    # strong red
}

SEARCH_URL = "https://gamma-api.polymarket.com/public-search"
TRADES_URL = "https://data-api.polymarket.com/trades"
DEFAULT_TRADE_FILE = "trades.json"
DEFAULT_REPORT_FILE = "report_path"
PRICE_RESOLUTION_THRESHOLD = 0.5

# ---------------------------------------------------
# REPORT GENERATION
# ---------------------------------------------------
def write_stats_report(
    report_path,
    target_market,
    resolved_side,
    trade_count,
    remaining_yes,
    remaining_no,
    final_value,
    total_spent,
    pnl,
    yes_buy_sh,
    yes_buy_cost,
    yes_sell_sh,
    yes_sell_cost,
    no_buy_sh,
    no_buy_cost,
    no_sell_sh,
    no_sell_cost,
    cum_yes_total,
    cum_no_total,
    cum_yes_cost_total,
    cum_no_cost_total,
    yes_curve,
    no_curve,
    net_curve,
    yes_sh_curve,
    no_sh_curve,
    net_sh_curve,
    prices,
    trades,
):
    # Safety checks for empty data
    if len(yes_curve) > 0:
        yes_peak_idx = int(np.argmax(yes_curve))
        no_peak_idx = int(np.argmax(no_curve))
        yes_sh_peak_idx = int(np.argmax(yes_sh_curve))
        no_sh_peak_idx = int(np.argmax(no_sh_curve))
        
        yes_peak_val = yes_curve[yes_peak_idx]
        no_peak_val = no_curve[no_peak_idx]
        yes_sh_peak_val = yes_sh_curve[yes_sh_peak_idx]
        no_sh_peak_val = no_sh_curve[no_sh_peak_idx]
        
        final_yes_exp = yes_curve[-1]
        final_yes_sh = yes_sh_curve[-1]
        final_no_exp = no_curve[-1]
        final_no_sh = no_sh_curve[-1]
        final_net_exp = net_curve[-1]
        final_net_sh = net_sh_curve[-1]
    else:
        yes_peak_idx = no_peak_idx = 0
        yes_sh_peak_idx = no_sh_peak_idx = 0
        yes_peak_val = no_peak_val = 0
        yes_sh_peak_val = no_sh_peak_val = 0
        final_yes_exp = final_yes_sh = 0
        final_no_exp = final_no_sh = 0
        final_net_exp = final_net_sh = 0

    # Calculate time range
    start_time = "N/A"
    end_time = "N/A"
    if trades:
        start_time = datetime.datetime.fromtimestamp(trades[0]['timestamp']).strftime('%Y-%m-%d %H:%M:%S')
        end_time = datetime.datetime.fromtimestamp(trades[-1]['timestamp']).strftime('%Y-%m-%d %H:%M:%S')
    
    min_price = min(prices) if prices else 0
    max_price = max(prices) if prices else 0

    lines = [
        f"MARKET: {target_market}",
        f"RESOLUTION: {resolved_side}",
        f"TRADES: {trade_count}",
        f"TIME RANGE: {start_time} to {end_time}",
        f"PRICE RANGE: {min_price:.2f} - {max_price:.2f}",
        "",
        "--- Position at resolution ---",
        f"Remaining YES shares: {remaining_yes:.2f}",
        f"Remaining NO shares:  {remaining_no:.2f}",
        f"Final value: $ {final_value:.2f}",
        f"Total spent (net exposure): $ {total_spent:.2f}",
        f"FINAL PNL: $ {pnl:.2f}",
        "",
        "--- Buy/Sell totals ---",
        f"YES buys:  {yes_buy_sh:.2f} sh / $ {yes_buy_cost:.2f}",
        f"YES sells: {yes_sell_sh:.2f} sh / $ {yes_sell_cost:.2f}",
        f"NO buys:   {no_buy_sh:.2f} sh / $ {no_buy_cost:.2f}",
        f"NO sells:  {no_sell_sh:.2f} sh / $ {no_sell_cost:.2f}",
        "",
        "--- Cumulative buys ---",
        f"YES cumulative: {cum_yes_total:.2f} sh / $ {cum_yes_cost_total:.2f}",
        f"NO cumulative:  {cum_no_total:.2f} sh / $ {cum_no_cost_total:.2f}",
        "",
        "--- Exposure peaks (trade index: earliest → latest) ---",
        f"YES dollar peak: $ {yes_peak_val:.2f} at trade #{yes_peak_idx + 1}",
        f"NO dollar peak:  $ {no_peak_val:.2f} at trade #{no_peak_idx + 1}",
        f"YES share peak:  {yes_sh_peak_val:.2f} sh at trade #{yes_sh_peak_idx + 1}",
        f"NO share peak:   {no_sh_peak_val:.2f} sh at trade #{no_sh_peak_idx + 1}",
        "",
        "--- Final exposure ---",
        f"YES exposure: $ {final_yes_exp:.2f} | {final_yes_sh:.2f} sh",
        f"NO exposure:  $ {final_no_exp:.2f} | {final_no_sh:.2f} sh",
        f"NET exposure: $ {final_net_exp:.2f} | {final_net_sh:.2f} sh",
    ]

    lines.append("")
    lines.append("--- Trades (Sorted by Timestamp) ---")
    lines.append("Idx | Time                | Type | Side | Price(c) |   Shares   |    Cost($)")
    lines.append("----+---------------------+------+-----+----------+------------+------------")

    for i, t in enumerate(trades):
        dt_str = datetime.datetime.fromtimestamp(t['timestamp']).strftime('%Y-%m-%d %H:%M:%S')
        lines.append(
            f"{i+1:3d} | {dt_str} | {t['type']:<4} | {t['side']:<4} | "
            f"{t['price']:8.2f} | {t['shares']:10.2f} | $ {t['cost']:9.2f}"
        )

    with open(report_path, "w") as f:
        f.write("\n".join(lines))


# ---------------------------------------------------
# DATA FETCHING
# ---------------------------------------------------
def search_market(query):
    """Return (event, market) for the first matching search result."""
    try:
        resp = requests.get(SEARCH_URL, params={"q": query}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        print(f"Error searching market: {exc}")
        return None, None

    events = data.get("events", []) if isinstance(data, dict) else []
    for event in events:
        markets = event.get("markets") or []
        if markets:
            return event, markets[0]
    return None, None


def fetch_trades(condition_id, user_address, page_limit=5000):
    """Fetch all trades for a condition/user with simple pagination."""
    all_trades = []
    offset = 0

    while True:
        params = {
            "limit": page_limit,
            "offset": offset,
            "takerOnly": "false",
            "market": condition_id,
            "user": user_address,
        }
        try:
            resp = requests.get(TRADES_URL, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            print(f"Error fetching trades: {exc}")
            return []

        if isinstance(data, dict):
            batch = data.get("trades", [])
        elif isinstance(data, list):
            batch = data
        else:
            batch = []

        all_trades.extend(batch)
        if len(batch) < page_limit:
            break
        offset += page_limit

    return all_trades


def prompt_resolved_side(current=None):
    """Return a valid resolved side, prompting if needed."""
    if current in {"YES", "NO"}:
        return current
    while True:
        side = input("Enter resolved side (YES/NO, blank = skip): ").strip().upper()
        if not side:
            return None
        if side in {"YES", "NO"}:
            return side
        print("Please enter YES or NO.")


def normalize_resolved_arg(value):
    if not value:
        return None
    value = value.strip().upper()
    if value in {"YES", "NO", "AUTO"}:
        return value
    return None


def infer_resolved_side_from_trades(trades, threshold=PRICE_RESOLUTION_THRESHOLD):
    """Infer resolved side from the most recent trade price."""
    if not trades:
        return None, None
    latest = max(trades, key=lambda t: t.get("timestamp", 0))
    price = float(latest.get("price", 0))
    outcome = latest.get("outcome", "").lower()

    if outcome not in {"up", "down"}:
        return None, latest

    # If price >= threshold, assume resolved toward that outcome; otherwise opposite.
    if price >= threshold:
        inferred = "YES" if outcome == "up" else "NO"
    else:
        inferred = "NO" if outcome == "up" else "YES"
    return inferred, latest


# ---------------------------------------------------
# MAIN
# ---------------------------------------------------
def main():
    # Resolved side can come from argv (YES/NO/AUTO) or be inferred later
    resolved_arg = normalize_resolved_arg(sys.argv[1] if len(sys.argv) > 1 else None)

    # Optional override: load from a provided JSON file instead of fetching
    json_file = sys.argv[2] if len(sys.argv) > 2 else None

    if json_file:
        try:
            with open(json_file, "r") as f:
                raw_data = json.load(f)
        except FileNotFoundError:
            print(f"Error: File '{json_file}' not found.")
            return
        except json.JSONDecodeError:
            print(f"Error: File '{json_file}' is not valid JSON.")
            return
        if not raw_data:
            print("No trades found.")
            return
        market_title = raw_data[0].get("title", "Unknown Market")
        condition_id = raw_data[0].get("conditionId", "")
    else:
        market_query = input("Enter market name to search: ").strip()
        if not market_query:
            print("Market name is required.")
            return

        event, market = search_market(market_query)
        if not market:
            print("No market found for that query.")
            return

        market_title = (
            market.get("question")
            or market.get("title")
            or event.get("title", "Unknown Market")
        )
        condition_id = market.get("conditionId") or ""
        print(f"Found market: {market_title}")
        print(f"Condition ID: {condition_id}")

        user_address = input("Enter user address to fetch trades: ").strip()
        if not user_address:
            print("User address is required.")
            return

        raw_data = fetch_trades(condition_id, user_address)
        if not raw_data:
            print("No trades returned for that user/market.")
            return

        with open(DEFAULT_TRADE_FILE, "w") as f:
            json.dump(raw_data, f, indent=2)
        print(f"Saved {len(raw_data)} trades to {DEFAULT_TRADE_FILE}")

    # Sort by timestamp
    raw_data.sort(key=lambda x: x.get("timestamp", 0))

    # Parse trades
    parsed = []
    target_market = market_title or raw_data[0].get("title", "Unknown Market")

    # Decide resolved side: explicit > inferred > prompt
    resolved_side = None
    if resolved_arg in {"YES", "NO"}:
        resolved_side = resolved_arg
    else:
        inferred, latest = infer_resolved_side_from_trades(raw_data)
        if inferred:
            resolved_side = inferred
            price = float(latest.get("price", 0))
            outcome = latest.get("outcome", "")
            ts = latest.get("timestamp", 0)
            print(f"Inferred resolved side: {resolved_side} (latest trade outcome {outcome} at price {price:.2f}, ts {ts})")
        else:
            if resolved_arg == "AUTO":
                print("Could not infer resolved side automatically.")
                return
            resolved_side = prompt_resolved_side(None)
            if not resolved_side:
                print("Resolved side is required.")
                return

    for item in raw_data:
        entry = {}
        raw_side = item.get("side", "BUY").upper()
        entry["type"] = "Buy" if raw_side == "BUY" else "Sell"
        entry["market"] = item.get("title", "")
        entry["side"] = item.get("outcome", "Up") 
        entry["price"] = float(item.get("price", 0)) * 100.0  # Convert to cents
        entry["shares"] = float(item.get("size", 0))
        entry["cost"] = float(item.get("price", 0)) * entry["shares"]
        entry["timestamp"] = int(item.get("timestamp", 0))
        parsed.append(entry)

    if not parsed:
        print("No entries found.")
        return

    prices = [e["price"] for e in parsed]

    # ---------------------------------------------------
    # EXPOSURE CURVES
    # ---------------------------------------------------
    yes_curve = []
    no_curve = []
    net_curve = []

    yes_sh_curve = []
    no_sh_curve = []
    net_sh_curve = []

    yes_exp = no_exp = 0
    yes_sh_exp = no_sh_exp = 0

    for e in parsed:
        # Dollar exposure
        if e["side"] == "Up":   # YES
            yes_exp += e["cost"] if e["type"] == "Buy" else -e["cost"]
        else:                   # NO
            no_exp += e["cost"] if e["type"] == "Buy" else -e["cost"]

        # Shares exposure
        if e["side"] == "Up":
            yes_sh_exp += e["shares"] if e["type"] == "Buy" else -e["shares"]
        else:
            no_sh_exp += e["shares"] if e["type"] == "Buy" else -e["shares"]

        yes_curve.append(yes_exp)
        no_curve.append(no_exp)
        net_curve.append(yes_exp + no_exp)

        yes_sh_curve.append(yes_sh_exp)
        no_sh_curve.append(no_sh_exp)
        net_sh_curve.append(yes_sh_exp + no_sh_exp)


    # ---------------------------------------------------
    # FINAL PNL CALC
    # ---------------------------------------------------
    remaining_yes = yes_sh_curve[-1]
    remaining_no = no_sh_curve[-1]
    total_spent = net_curve[-1]

    if resolved_side == "YES":
        final_value = remaining_yes * 1.0
    else:
        final_value = remaining_no * 1.0

    pnl = final_value - total_spent

    pnl_text = (
        f"MARKET RESOLVED: {resolved_side}\n\n"
        f"Remaining YES shares: {remaining_yes:.2f}\n"
        f"Remaining NO shares:  {remaining_no:.2f}\n\n"
        f"Final Value: $ {final_value:.2f}\n"
        f"Total Spent (net exposure): $ {total_spent:.2f}\n\n"
        f"FINAL PNL: $ {pnl:.2f}"
    )

    # ---------------------------------------------------
    # PREPARE GLOBAL TOTALS
    # ---------------------------------------------------
    yes_buy_sh = yes_buy_cost = 0
    yes_sell_sh = yes_sell_cost = 0
    no_buy_sh = no_buy_cost = 0
    no_sell_sh = no_sell_cost = 0

    # Arrays for cumulative plotting
    raw_vol_yes = []
    raw_vol_no = []
    raw_cost_yes = []
    raw_cost_no = []

    for e in parsed:
        is_yes = (e["side"] == "Up")
        is_buy = (e["type"] == "Buy")
        
        if is_buy:
            if is_yes:
                yes_buy_sh += e["shares"]
                yes_buy_cost += e["cost"]
                raw_vol_yes.append(e["shares"])
                raw_vol_no.append(0)
                raw_cost_yes.append(e["cost"])
                raw_cost_no.append(0)
            else:
                no_buy_sh += e["shares"]
                no_buy_cost += e["cost"]
                raw_vol_yes.append(0)
                raw_vol_no.append(e["shares"])
                raw_cost_yes.append(0)
                raw_cost_no.append(e["cost"])
        else:
            # Sell
            raw_vol_yes.append(0)
            raw_vol_no.append(0)
            raw_cost_yes.append(0)
            raw_cost_no.append(0)
            if is_yes:
                yes_sell_sh += e["shares"]
                yes_sell_cost += e["cost"]
            else:
                no_sell_sh += e["shares"]
                no_sell_cost += e["cost"]

    cum_yes = np.cumsum(raw_vol_yes)
    cum_no = np.cumsum(raw_vol_no)
    cum_yes_cost = np.cumsum(raw_cost_yes)
    cum_no_cost = np.cumsum(raw_cost_no)

    cum_yes_total = cum_yes[-1] if len(cum_yes) > 0 else 0
    cum_no_total = cum_no[-1] if len(cum_no) > 0 else 0
    cum_yes_cost_total = cum_yes_cost[-1] if len(cum_yes_cost) > 0 else 0
    cum_no_cost_total = cum_no_cost[-1] if len(cum_no_cost) > 0 else 0

    # ---------------------------------------------------
    # PLOT SETUP
    # ---------------------------------------------------
    unique_timestamps = sorted(list(set(t['timestamp'] for t in parsed)))
    ts_map = {ts: i for i, ts in enumerate(unique_timestamps)}
    
    x_indices = [ts_map[e['timestamp']] for e in parsed]

    fig, (ax1, ax2, ax3, ax4) = plt.subplots(
        4, 1, figsize=(16, 14.5),
        gridspec_kw={'height_ratios': [3, 1.3, 1.1, 1.1]}
    )
    fig.subplots_adjust(hspace=0.45, bottom=0.2)


    # ---------------------------------------------------
    # TOP: BUY/SELL SCATTER (GROUPED BUBBLE VIEW)
    # ---------------------------------------------------
    grouped_trades = {}
    for i, e in enumerate(parsed):
        x_idx = ts_map[e['timestamp']]
        if x_idx not in grouped_trades:
            grouped_trades[x_idx] = []
        grouped_trades[x_idx].append(e)

    next_up = True 

    for x_idx in sorted(grouped_trades.keys()):
        group = grouped_trades[x_idx]
        avg_price = sum(t["price"] for t in group) / len(group)
        
        if len(group) == 1:
            # SINGLE TRADE
            e = group[0]
            style_key = (e["type"], e["side"])
            if style_key in STYLES:
                color, marker, label = STYLES[style_key]
            else:
                color, marker, label = ("gray", "o", "Unknown")
            
            ax1.scatter(x_idx, e["price"], color=color, marker=marker,
                        s=60, linewidths=2.5 if marker=="x" else 1.0,
                        alpha=0.9, zorder=5)
            
            direction = 1 if next_up else -1
            next_up = not next_up
            candle_len = 15 * 0.7 # 30% smaller
            end_y = e["price"] + direction * candle_len
            
            ax1.vlines(x_idx, e["price"], end_y, colors=color, linewidth=1.5, alpha=0.6)
            
            # Format to 2 decimals for Shares and Cost
            label_text = f"{e['shares']:.2f}sh\n${e['cost']:.2f}"
            
            ax1.annotate(
                label_text,
                xy=(x_idx, end_y),
                xytext=(0, direction * 2),
                textcoords="offset points",
                ha="center", va="bottom" if direction > 0 else "top",
                fontsize=7,
                bbox=dict(boxstyle="round,pad=0.2", fc="white", alpha=0.7, ec="none")
            )
            
        else:
            # MULTIPLE TRADES
            count = len(group)
            first_e = group[0]
            same_side = all((t["type"] == first_e["type"] and t["side"] == first_e["side"]) for t in group)
            
            if same_side:
                style_key = (first_e["type"], first_e["side"])
                color, _, _ = STYLES.get(style_key, ("gray", "o", ""))
            else:
                color = "#1f77b4" 

            ax1.scatter(x_idx, avg_price, color="white", marker="o", s=300, edgecolors=color, linewidth=2, zorder=5)
            ax1.text(x_idx, avg_price, str(count), ha="center", va="center", fontsize=9, fontweight="bold", color=color, zorder=6)
            
            direction = 1 if next_up else -1
            next_up = not next_up
            
            info_lines = []
            for idx, t in enumerate(group):
                if idx < 5:
                    # Format to 2 decimals for Shares and Cost
                    info_lines.append(f"{t['shares']:.2f}sh ${t['cost']:.2f} ({t['side']})")
                else:
                    remaining = len(group) - 5
                    info_lines.append(f"...+ {remaining} more")
                    break
            
            box_text = "\n".join(info_lines)
            
            # 30% SMALLER LINE logic
            raw_len = 25 + (len(info_lines) * 5)
            candle_len = raw_len * 0.7 
            
            end_y = avg_price + direction * candle_len
            
            ax1.vlines(x_idx, avg_price, end_y, colors=color, linewidth=2, alpha=0.6, linestyles="dotted")
            
            ax1.annotate(
                box_text,
                xy=(x_idx, end_y),       
                xytext=(0, direction * 2), 
                textcoords="offset points",
                ha="center", va="bottom" if direction > 0 else "top",
                fontsize=6,
                bbox=dict(boxstyle="round,pad=0.3", fc="white", alpha=0.85, ec=color)
            )

    ax1.set_title(f"Trades for {target_market}")
    ax1.set_ylabel("Price (cents)")
    
    def time_formatter(x, pos):
        idx = int(x)
        if 0 <= idx < len(unique_timestamps):
            ts = unique_timestamps[idx]
            return datetime.datetime.fromtimestamp(ts).strftime('%H:%M:%S')
        return ""

    ax1.xaxis.set_major_locator(ticker.MaxNLocator(nbins=12))
    ax1.xaxis.set_major_formatter(ticker.FuncFormatter(time_formatter))
    ax1.set_yticks(range(0, 101, 10))
    ax1.grid(axis='y', linestyle='--', alpha=0.3)

    vol_yes_per_ts = [0.0] * len(unique_timestamps)
    vol_no_per_ts = [0.0] * len(unique_timestamps)
    
    for x_idx, group in grouped_trades.items():
        for t in group:
            if t["type"] == "Buy":
                if t["side"] == "Up":
                    vol_yes_per_ts[x_idx] += t["shares"]
                else:
                    vol_no_per_ts[x_idx] += t["shares"]

    x_range = np.arange(len(unique_timestamps))
    vol_ax = ax1.inset_axes([0, 0.0, 1.0, 0.2], sharex=ax1)
    vol_ax.patch.set_alpha(0)
    vol_ax.bar(x_range - 0.35/2, vol_yes_per_ts, width=0.35,
               color="green", alpha=0.18, label="Buy YES volume")
    vol_ax.bar(x_range + 0.35/2, vol_no_per_ts, width=0.35,
               color="red", alpha=0.18, label="Buy NO volume")
    vol_ax.set_yticks([])
    vol_ax.set_xticks([])
    vol_ax.set_xlim(-0.5, len(unique_timestamps) - 0.5)

    # ---------------------------------------------------
    # SECOND: CUMULATIVE BUY SHARES + COST
    # ---------------------------------------------------
    ax2.plot(x_indices, cum_yes, color="green", alpha=0.3, linewidth=1, label="Cum Buy YES (sh)")
    ax2.fill_between(x_indices, cum_yes, color="green", alpha=0.1)
    
    ax2.plot(x_indices, cum_no, color="red", alpha=0.3, linewidth=1, label="Cum Buy NO (sh)")
    ax2.fill_between(x_indices, cum_no, color="red", alpha=0.1)
    
    ax2.set_ylabel("Cumulative buy volume (sh)")
    ax2.grid(axis='y', alpha=0.2)
    ax2.set_xticks([])
    ax2.set_title("Cumulative Buys (shares + dollars)")

    max_cum = max(cum_yes.max() if len(cum_yes) else 0, cum_no.max() if len(cum_no) else 0)
    ax2.set_ylim(0, max_cum * 1.15 + 1e-6)

    ax2_cost = ax2.twinx()
    ax2_cost.plot(x_indices, cum_yes_cost, color="green", linewidth=1.8,
                  linestyle="--", alpha=0.7, label="Cumulative Buy YES ($)")
    ax2_cost.plot(x_indices, cum_no_cost, color="red", linewidth=1.8,
                  linestyle="--", alpha=0.7, label="Cumulative Buy NO ($)")
    ax2_cost.set_ylabel("Cumulative buy cost ($)", color="gray", fontsize=9)
    ax2_cost.tick_params(axis='y', labelsize=8, colors="gray")
    ax2_cost.spines['right'].set_alpha(0.3)
    handles2, labels2 = ax2_cost.get_legend_handles_labels()
    handles1, labels1 = ax2.get_legend_handles_labels()
    ax2.legend(handles1 + handles2, labels1 + labels2, loc="upper left")

    cum_stats_text = (
        f"YES: {cum_yes_total:.2f} sh / $ {cum_yes_cost_total:.2f}\n"
        f"NO:  {cum_no_total:.2f} sh / $ {cum_no_cost_total:.2f}"
    )
    ax2.text(
        0.01, 0.02, cum_stats_text,
        transform=ax2.transAxes,
        ha="left", va="bottom",
        fontsize=9,
        bbox=dict(boxstyle="round,pad=0.3", fc="white", alpha=0.8, ec="gray")
    )

    # ---------------------------------------------------
    # THIRD: DOLLAR EXPOSURE
    # ---------------------------------------------------
    ax3.grid(alpha=0.3)
    ax3.plot(x_indices, yes_curve, color="green", linewidth=2, label="YES Exposure ($)")
    ax3.plot(x_indices, no_curve, color="red", linewidth=2, label="NO Exposure ($)")
    ax3.plot(x_indices, net_curve, color="blue", linewidth=2, label="NET Exposure ($ total)")

    if len(yes_curve) > 0:
        yes_peak = int(np.argmax(yes_curve))
        no_peak = int(np.argmax(no_curve))
        ax3.annotate("YES peak $", (x_indices[yes_peak], yes_curve[yes_peak]), xytext=(0, -20),
                     textcoords="offset points", ha='center', arrowprops=dict(arrowstyle="->", color="green"), color="green")
        ax3.annotate("NO peak $", (x_indices[no_peak], no_curve[no_peak]), xytext=(0, -20),
                     textcoords="offset points", ha='center', arrowprops=dict(arrowstyle="->", color="red"), color="red")
        
        last_x = x_indices[-1]
        ax3.annotate(f"$ {yes_curve[-1]:.2f}", (last_x, yes_curve[-1]), xytext=(15, 0), textcoords="offset points", color="green")
        ax3.annotate(f"$ {no_curve[-1]:.2f}", (last_x, no_curve[-1]), xytext=(15, 0), textcoords="offset points", color="red")
        ax3.annotate(f"$ {net_curve[-1]:.2f}", (last_x, net_curve[-1]), xytext=(15, 0), textcoords="offset points", color="blue")

    ax3.set_title("Dollar Exposure")
    ax3.set_ylabel("Exposure ($)")
    ax3.set_xticks([])
    ax3.legend(loc="upper left")

    summary = (
        f"YES (Up)  Buy: {yes_buy_sh:.2f} sh ($ {yes_buy_cost:.2f}) "
        f" | Sell: {yes_sell_sh:.2f} sh ($ {yes_sell_cost:.2f})\n"
        f"NO  (Down) Buy: {no_buy_sh:.2f} sh ($ {no_buy_cost:.2f}) "
        f" | Sell: {no_sell_sh:.2f} sh ($ {no_sell_cost:.2f})"
    )
    fig.text(0.01, 0.01, summary, ha="left", va="bottom", fontsize=11,
             bbox=dict(facecolor="white", alpha=0.75, edgecolor="black"))
    fig.text(0.99, 0.06, pnl_text, ha="right", va="top", fontsize=12,
             bbox=dict(facecolor="white", alpha=0.75, edgecolor="black"))


    # ---------------------------------------------------
    # BOTTOM: SHARES EXPOSURE
    # ---------------------------------------------------
    ax4.grid(alpha=0.3)
    ax4.plot(x_indices, yes_sh_curve, color="green", linewidth=2, label="YES Exposure (shares)")
    ax4.plot(x_indices, no_sh_curve, color="red", linewidth=2, label="NO Exposure (shares)")
    ax4.plot(x_indices, net_sh_curve, color="blue", linewidth=2, label="NET Exposure (shares)")

    if len(yes_sh_curve) > 0:
        yes_sh_peak = int(np.argmax(yes_sh_curve))
        no_sh_peak = int(np.argmax(no_sh_curve))
        ax4.annotate("YES peak sh", (x_indices[yes_sh_peak], yes_sh_curve[yes_sh_peak]), xytext=(0, -20),
                     textcoords="offset points", ha='center', arrowprops=dict(arrowstyle="->", color="green"), color="green")
        ax4.annotate("NO peak sh", (x_indices[no_sh_peak], no_sh_curve[no_sh_peak]), xytext=(0, -20),
                     textcoords="offset points", ha='center', arrowprops=dict(arrowstyle="->", color="red"), color="red")
        
        ax4.annotate(f"{yes_sh_curve[-1]:.2f} sh", (last_x, yes_sh_curve[-1]), xytext=(15, 0), textcoords="offset points", color="green")
        ax4.annotate(f"{no_sh_curve[-1]:.2f} sh", (last_x, no_sh_curve[-1]), xytext=(15, 0), textcoords="offset points", color="red")
        ax4.annotate(f"{net_sh_curve[-1]:.2f} sh", (last_x, net_sh_curve[-1]), xytext=(15, 0), textcoords="offset points", color="blue")

    ax4.set_title("Shares Exposure")
    ax4.set_ylabel("Shares")
    
    ax4.xaxis.set_major_locator(ticker.MaxNLocator(nbins=12))
    ax4.xaxis.set_major_formatter(ticker.FuncFormatter(time_formatter))
    plt.setp(ax4.get_xticklabels(), rotation=30, ha='right')
    
    ax4.legend(loc="upper left")

    # Sync X limits
    xlim_range = (-0.5, len(unique_timestamps) - 0.5)
    for axis in (ax1, ax2, ax3, ax4):
        axis.set_xlim(*xlim_range)

    plt.tight_layout()
    plt.savefig("chart.png", dpi=200, bbox_inches="tight")
    plt.close('all')

    write_stats_report(
        DEFAULT_REPORT_FILE,
        target_market,
        resolved_side,
        len(parsed),
        remaining_yes,
        remaining_no,
        final_value,
        total_spent,
        pnl,
        yes_buy_sh,
        yes_buy_cost,
        yes_sell_sh,
        yes_sell_cost,
        no_buy_sh,
        no_buy_cost,
        no_sell_sh,
        no_sell_cost,
        cum_yes_total,
        cum_no_total,
        cum_yes_cost_total,
        cum_no_cost_total,
        yes_curve,
        no_curve,
        net_curve,
        yes_sh_curve,
        no_sh_curve,
        net_sh_curve,
        prices,
        parsed,
    )

    print("Chart saved as chart.png")
    # print(f"Stats report saved as {DEFAULT_REPORT_FILE}")


if __name__ == "__main__":
    main()
