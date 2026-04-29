"""
mlb_2025_tiers.py — Offline 2025 dataset (150 batters + 33 pitchers).

Three-tier batter pools ranked by 2025 HR totals, with Statcast/FanGraphs
power metrics. Used as fallback data when live APIs are unavailable and for
backtesting simulations.
"""

# ──────────────────────────────────────────────────────────────────────────────
# Tier 1 — Chalk (Top 50 HR hitters, ~18-60 HR) — 1 pt per HR
# ──────────────────────────────────────────────────────────────────────────────
TIER_1_BATTERS = [
    {"name": "Aaron Judge",        "team": "NYY", "bats": "R", "hr": 52, "barrel_pct": 16.2, "exit_velo": 93.4, "hr_fb_pct": 21.5, "iso": 0.295, "woba": 0.385, "player_id": 592450},
    {"name": "Shohei Ohtani",      "team": "LAD", "bats": "L", "hr": 48, "barrel_pct": 14.8, "exit_velo": 91.9, "hr_fb_pct": 19.8, "iso": 0.271, "woba": 0.370, "player_id": 660271},
    {"name": "Kyle Schwarber",     "team": "PHI", "bats": "L", "hr": 45, "barrel_pct": 13.5, "exit_velo": 91.2, "hr_fb_pct": 18.2, "iso": 0.258, "woba": 0.362, "player_id": 656941},
    {"name": "Pete Alonso",        "team": "NYM", "bats": "R", "hr": 44, "barrel_pct": 12.9, "exit_velo": 90.8, "hr_fb_pct": 17.5, "iso": 0.251, "woba": 0.355, "player_id": 624413},
    {"name": "Gunnar Henderson",   "team": "BAL", "bats": "S", "hr": 42, "barrel_pct": 12.4, "exit_velo": 90.1, "hr_fb_pct": 16.9, "iso": 0.244, "woba": 0.348, "player_id": 683002},
    {"name": "Marcell Ozuna",      "team": "ATL", "bats": "R", "hr": 41, "barrel_pct": 11.8, "exit_velo": 89.5, "hr_fb_pct": 16.3, "iso": 0.238, "woba": 0.342, "player_id": 542303},
    {"name": "Juan Soto",          "team": "NYM", "bats": "L", "hr": 40, "barrel_pct": 11.5, "exit_velo": 89.2, "hr_fb_pct": 15.8, "iso": 0.235, "woba": 0.380, "player_id": 665742},
    {"name": "Yordan Alvarez",     "team": "HOU", "bats": "L", "hr": 40, "barrel_pct": 11.2, "exit_velo": 89.1, "hr_fb_pct": 15.7, "iso": 0.233, "woba": 0.375, "player_id": 670541},
    {"name": "Rafael Devers",      "team": "SF",  "bats": "L", "hr": 39, "barrel_pct": 10.9, "exit_velo": 88.6, "hr_fb_pct": 15.4, "iso": 0.229, "woba": 0.368, "player_id": 541629},
    # Anthony Santander — OUT (torn ACL, March 2026)
    {"name": "Jose Altuve",        "team": "HOU", "bats": "R", "hr": 38, "barrel_pct": 10.4, "exit_velo": 88.1, "hr_fb_pct": 14.9, "iso": 0.223, "woba": 0.352, "player_id": 514888},
    {"name": "Matt Olson",         "team": "ATL", "bats": "L", "hr": 38, "barrel_pct": 10.2, "exit_velo": 87.9, "hr_fb_pct": 14.7, "iso": 0.220, "woba": 0.348, "player_id": 621566},
    {"name": "Giancarlo Stanton",  "team": "NYY", "bats": "R", "hr": 37, "barrel_pct": 9.9, "exit_velo": 87.5, "hr_fb_pct": 14.4, "iso": 0.216, "woba": 0.340, "player_id": 519317},
    {"name": "Corey Seager",       "team": "TEX", "bats": "L", "hr": 37, "barrel_pct": 9.7, "exit_velo": 87.2, "hr_fb_pct": 14.2, "iso": 0.213, "woba": 0.352, "player_id": 606229},
    {"name": "Adolis Garcia",      "team": "TEX", "bats": "R", "hr": 36, "barrel_pct": 9.4, "exit_velo": 86.8, "hr_fb_pct": 13.9, "iso": 0.209, "woba": 0.344, "player_id": 666969},
    {"name": "Jazz Chisholm Jr.",   "team": "NYY", "bats": "L", "hr": 36, "barrel_pct": 9.2, "exit_velo": 86.5, "hr_fb_pct": 13.7, "iso": 0.207, "woba": 0.338, "player_id": 665862},
    {"name": "Willy Adames",       "team": "SF",  "bats": "R", "hr": 35, "barrel_pct": 8.9, "exit_velo": 86.2, "hr_fb_pct": 13.5, "iso": 0.204, "woba": 0.334, "player_id": 642715},
    {"name": "Mitch Garver",       "team": "SEA", "bats": "R", "hr": 34, "barrel_pct": 8.7, "exit_velo": 85.9, "hr_fb_pct": 13.3, "iso": 0.201, "woba": 0.330, "player_id": 641598},
    {"name": "Bryce Harper",       "team": "PHI", "bats": "L", "hr": 34, "barrel_pct": 8.5, "exit_velo": 85.6, "hr_fb_pct": 13.1, "iso": 0.198, "woba": 0.360, "player_id": 547180},
    {"name": "Brent Rooker",       "team": "OAK", "bats": "R", "hr": 33, "barrel_pct": 8.3, "exit_velo": 85.3, "hr_fb_pct": 12.9, "iso": 0.195, "woba": 0.325, "player_id": 667670},
    {"name": "Freddie Freeman",    "team": "LAD", "bats": "L", "hr": 32, "barrel_pct": 8.1, "exit_velo": 85.0, "hr_fb_pct": 12.7, "iso": 0.192, "woba": 0.365, "player_id": 518692},
    {"name": "Salvador Perez",     "team": "KC",  "bats": "R", "hr": 32, "barrel_pct": 7.9, "exit_velo": 84.8, "hr_fb_pct": 12.5, "iso": 0.189, "woba": 0.320, "player_id": 521692},
    {"name": "Francisco Lindor",   "team": "NYM", "bats": "S", "hr": 31, "barrel_pct": 7.7, "exit_velo": 84.5, "hr_fb_pct": 12.3, "iso": 0.187, "woba": 0.355, "player_id": 596019},
    {"name": "Marcus Semien",      "team": "TEX", "bats": "R", "hr": 30, "barrel_pct": 7.5, "exit_velo": 84.2, "hr_fb_pct": 12.0, "iso": 0.184, "woba": 0.340, "player_id": 543760},
    {"name": "Cody Bellinger",     "team": "NYY", "bats": "L", "hr": 30, "barrel_pct": 7.3, "exit_velo": 84.0, "hr_fb_pct": 11.8, "iso": 0.181, "woba": 0.345, "player_id": 641355},
    {"name": "George Springer",    "team": "TOR", "bats": "R", "hr": 29, "barrel_pct": 7.1, "exit_velo": 83.7, "hr_fb_pct": 11.5, "iso": 0.178, "woba": 0.335, "player_id": 543807},
    {"name": "Matt Chapman",       "team": "SF",  "bats": "R", "hr": 29, "barrel_pct": 6.9, "exit_velo": 83.5, "hr_fb_pct": 11.3, "iso": 0.176, "woba": 0.342, "player_id": 656305},
    {"name": "Mookie Betts",       "team": "LAD", "bats": "R", "hr": 28, "barrel_pct": 6.7, "exit_velo": 83.2, "hr_fb_pct": 11.1, "iso": 0.173, "woba": 0.370, "player_id": 605141},
    {"name": "Vladimir Guerrero Jr.", "team": "TOR", "bats": "R", "hr": 28, "barrel_pct": 6.5, "exit_velo": 83.0, "hr_fb_pct": 10.9, "iso": 0.170, "woba": 0.355, "player_id": 665489},
    {"name": "Teoscar Hernandez",  "team": "LAD", "bats": "R", "hr": 27, "barrel_pct": 6.3, "exit_velo": 82.8, "hr_fb_pct": 10.7, "iso": 0.168, "woba": 0.330, "player_id": 606192},
    {"name": "Bobby Witt Jr.",     "team": "KC",  "bats": "R", "hr": 27, "barrel_pct": 6.1, "exit_velo": 82.5, "hr_fb_pct": 10.5, "iso": 0.165, "woba": 0.358, "player_id": 677951},
    {"name": "Alex Bregman",       "team": "BOS", "bats": "R", "hr": 26, "barrel_pct": 5.9, "exit_velo": 82.3, "hr_fb_pct": 10.3, "iso": 0.163, "woba": 0.345, "player_id": 608324},
    {"name": "Manny Machado",      "team": "SD",  "bats": "R", "hr": 26, "barrel_pct": 5.7, "exit_velo": 82.0, "hr_fb_pct": 10.1, "iso": 0.160, "woba": 0.340, "player_id": 592518},
    {"name": "Austin Riley",       "team": "ATL", "bats": "R", "hr": 25, "barrel_pct": 5.5, "exit_velo": 81.8, "hr_fb_pct": 9.9, "iso": 0.158, "woba": 0.335, "player_id": 663586},
    {"name": "Nolan Arenado",      "team": "HOU", "bats": "R", "hr": 25, "barrel_pct": 5.3, "exit_velo": 81.5, "hr_fb_pct": 9.7, "iso": 0.155, "woba": 0.332, "player_id": 571448},
    {"name": "Fernando Tatis Jr.", "team": "SD",  "bats": "R", "hr": 24, "barrel_pct": 5.2, "exit_velo": 81.3, "hr_fb_pct": 9.5, "iso": 0.153, "woba": 0.348, "player_id": 665487},
    {"name": "Mike Trout",         "team": "LAA", "bats": "R", "hr": 24, "barrel_pct": 5.0, "exit_velo": 81.0, "hr_fb_pct": 9.3, "iso": 0.150, "woba": 0.365, "player_id": 545361},
    {"name": "Ozzie Albies",       "team": "ATL", "bats": "S", "hr": 23, "barrel_pct": 4.8, "exit_velo": 80.8, "hr_fb_pct": 9.1, "iso": 0.148, "woba": 0.330, "player_id": 645277},
    {"name": "Jose Ramirez",       "team": "CLE", "bats": "S", "hr": 23, "barrel_pct": 4.6, "exit_velo": 80.5, "hr_fb_pct": 8.9, "iso": 0.145, "woba": 0.358, "player_id": 608070},
    {"name": "Trea Turner",        "team": "PHI", "bats": "R", "hr": 22, "barrel_pct": 4.4, "exit_velo": 80.3, "hr_fb_pct": 8.7, "iso": 0.143, "woba": 0.350, "player_id": 607208},
    {"name": "Will Smith",         "team": "LAD", "bats": "R", "hr": 22, "barrel_pct": 4.2, "exit_velo": 80.0, "hr_fb_pct": 8.5, "iso": 0.140, "woba": 0.338, "player_id": 669257},
    {"name": "Eloy Jimenez",       "team": "CWS", "bats": "R", "hr": 21, "barrel_pct": 4.0, "exit_velo": 79.8, "hr_fb_pct": 8.3, "iso": 0.138, "woba": 0.315, "player_id": 650391},
    {"name": "Julio Rodriguez",    "team": "SEA", "bats": "R", "hr": 21, "barrel_pct": 3.9, "exit_velo": 79.6, "hr_fb_pct": 8.1, "iso": 0.136, "woba": 0.328, "player_id": 677594},
    {"name": "Ryan McMahon",       "team": "COL", "bats": "L", "hr": 20, "barrel_pct": 3.7, "exit_velo": 79.3, "hr_fb_pct": 7.9, "iso": 0.133, "woba": 0.318, "player_id": 641857},
    {"name": "J.P. Crawford",      "team": "SEA", "bats": "L", "hr": 20, "barrel_pct": 3.5, "exit_velo": 79.1, "hr_fb_pct": 7.7, "iso": 0.131, "woba": 0.322, "player_id": 641487},
    {"name": "Yandy Diaz",         "team": "TB",  "bats": "R", "hr": 19, "barrel_pct": 3.3, "exit_velo": 78.8, "hr_fb_pct": 7.5, "iso": 0.128, "woba": 0.345, "player_id": 650490},
    {"name": "Gleyber Torres",     "team": "DET", "bats": "R", "hr": 19, "barrel_pct": 3.2, "exit_velo": 78.6, "hr_fb_pct": 7.3, "iso": 0.126, "woba": 0.310, "player_id": 650402},
    {"name": "Willson Contreras",   "team": "STL", "bats": "R", "hr": 18, "barrel_pct": 3.0, "exit_velo": 78.4, "hr_fb_pct": 7.1, "iso": 0.124, "woba": 0.325, "player_id": 575929},
    {"name": "Ian Happ",           "team": "CHC", "bats": "S", "hr": 18, "barrel_pct": 2.9, "exit_velo": 78.2, "hr_fb_pct": 6.9, "iso": 0.122, "woba": 0.328, "player_id": 664023},
    {"name": "Ke'Bryan Hayes",     "team": "PIT", "bats": "R", "hr": 18, "barrel_pct": 2.7, "exit_velo": 78.0, "hr_fb_pct": 6.7, "iso": 0.120, "woba": 0.315, "player_id": 663647},
]

# ──────────────────────────────────────────────────────────────────────────────
# Tier 2 — Mid-Range (Ranked 51-100, ~13-17 HR) — 3 pts per HR
# ──────────────────────────────────────────────────────────────────────────────
TIER_2_BATTERS = [
    {"name": "Elly De La Cruz",    "team": "CIN", "bats": "S", "hr": 17, "barrel_pct": 4.5, "exit_velo": 81.0, "hr_fb_pct": 8.0, "iso": 0.165, "woba": 0.325, "player_id": 682829},
    {"name": "Jackson Chourio",    "team": "MIL", "bats": "R", "hr": 17, "barrel_pct": 4.3, "exit_velo": 80.8, "hr_fb_pct": 7.8, "iso": 0.162, "woba": 0.320, "player_id": 694192},
    {"name": "CJ Abrams",         "team": "WSH", "bats": "L", "hr": 17, "barrel_pct": 4.1, "exit_velo": 80.5, "hr_fb_pct": 7.6, "iso": 0.160, "woba": 0.318, "player_id": 682928},
    {"name": "Oneil Cruz",         "team": "PIT", "bats": "L", "hr": 17, "barrel_pct": 5.5, "exit_velo": 81.5, "hr_fb_pct": 8.2, "iso": 0.170, "woba": 0.310, "player_id": 665833},
    {"name": "Christian Walker",   "team": "HOU", "bats": "R", "hr": 16, "barrel_pct": 3.9, "exit_velo": 80.2, "hr_fb_pct": 7.4, "iso": 0.157, "woba": 0.330, "player_id": 572233},
    {"name": "Tyler O'Neill",     "team": "BOS", "bats": "R", "hr": 16, "barrel_pct": 5.3, "exit_velo": 81.2, "hr_fb_pct": 8.5, "iso": 0.175, "woba": 0.315, "player_id": 641933},
    {"name": "Seiya Suzuki",       "team": "CHC", "bats": "R", "hr": 16, "barrel_pct": 3.7, "exit_velo": 80.0, "hr_fb_pct": 7.2, "iso": 0.155, "woba": 0.340, "player_id": 673548},
    {"name": "Bo Bichette",        "team": "TOR", "bats": "R", "hr": 16, "barrel_pct": 3.5, "exit_velo": 79.7, "hr_fb_pct": 7.0, "iso": 0.152, "woba": 0.322, "player_id": 666182},
    {"name": "William Contreras",  "team": "MIL", "bats": "R", "hr": 16, "barrel_pct": 3.3, "exit_velo": 79.5, "hr_fb_pct": 6.8, "iso": 0.150, "woba": 0.335, "player_id": 661388},
    {"name": "Isaac Paredes",      "team": "CHC", "bats": "R", "hr": 16, "barrel_pct": 3.1, "exit_velo": 79.2, "hr_fb_pct": 6.6, "iso": 0.147, "woba": 0.328, "player_id": 670623},
    {"name": "Jake Burger",        "team": "MIA", "bats": "R", "hr": 15, "barrel_pct": 5.0, "exit_velo": 80.8, "hr_fb_pct": 7.8, "iso": 0.168, "woba": 0.305, "player_id": 669394},
    {"name": "Vinnie Pasquantino", "team": "KC",  "bats": "L", "hr": 15, "barrel_pct": 2.9, "exit_velo": 79.0, "hr_fb_pct": 6.4, "iso": 0.145, "woba": 0.332, "player_id": 686469},
    {"name": "Colton Cowser",      "team": "BAL", "bats": "L", "hr": 15, "barrel_pct": 4.8, "exit_velo": 80.5, "hr_fb_pct": 7.5, "iso": 0.163, "woba": 0.310, "player_id": 681297},
    {"name": "Lars Nootbaar",      "team": "STL", "bats": "L", "hr": 15, "barrel_pct": 2.7, "exit_velo": 78.8, "hr_fb_pct": 6.2, "iso": 0.142, "woba": 0.325, "player_id": 663457},
    {"name": "Spencer Torkelson",  "team": "DET", "bats": "R", "hr": 15, "barrel_pct": 4.6, "exit_velo": 80.2, "hr_fb_pct": 7.3, "iso": 0.160, "woba": 0.300, "player_id": 679529},
    {"name": "Masataka Yoshida",   "team": "BOS", "bats": "L", "hr": 14, "barrel_pct": 2.5, "exit_velo": 78.5, "hr_fb_pct": 6.0, "iso": 0.140, "woba": 0.345, "player_id": 807799},
    {"name": "Luis Arraez",        "team": "SF",  "bats": "L", "hr": 14, "barrel_pct": 2.3, "exit_velo": 78.3, "hr_fb_pct": 5.8, "iso": 0.137, "woba": 0.355, "player_id": 650333},
    {"name": "Brendan Donovan",    "team": "STL", "bats": "L", "hr": 14, "barrel_pct": 2.1, "exit_velo": 78.0, "hr_fb_pct": 5.6, "iso": 0.135, "woba": 0.328, "player_id": 680977},
    {"name": "Michael Harris II",  "team": "ATL", "bats": "L", "hr": 14, "barrel_pct": 3.8, "exit_velo": 79.5, "hr_fb_pct": 6.5, "iso": 0.148, "woba": 0.320, "player_id": 671739},
    {"name": "Brandon Nimmo",      "team": "NYM", "bats": "L", "hr": 14, "barrel_pct": 2.0, "exit_velo": 77.8, "hr_fb_pct": 5.5, "iso": 0.132, "woba": 0.345, "player_id": 607043},
    {"name": "Tommy Edman",        "team": "LAD", "bats": "S", "hr": 14, "barrel_pct": 1.8, "exit_velo": 77.5, "hr_fb_pct": 5.3, "iso": 0.130, "woba": 0.320, "player_id": 669242},
    {"name": "Tyler Soderstrom",   "team": "OAK", "bats": "L", "hr": 14, "barrel_pct": 4.2, "exit_velo": 79.8, "hr_fb_pct": 6.8, "iso": 0.155, "woba": 0.305, "player_id": 691159},
    {"name": "Xander Bogaerts",    "team": "SD",  "bats": "R", "hr": 13, "barrel_pct": 1.7, "exit_velo": 77.3, "hr_fb_pct": 5.1, "iso": 0.128, "woba": 0.335, "player_id": 593428},
    {"name": "Wilyer Abreu",       "team": "BOS", "bats": "L", "hr": 13, "barrel_pct": 3.5, "exit_velo": 79.0, "hr_fb_pct": 6.3, "iso": 0.145, "woba": 0.318, "player_id": 673899},
    {"name": "Alec Burleson",      "team": "STL", "bats": "L", "hr": 13, "barrel_pct": 1.5, "exit_velo": 77.0, "hr_fb_pct": 4.9, "iso": 0.125, "woba": 0.315, "player_id": 676475},
    {"name": "Daulton Varsho",     "team": "TOR", "bats": "L", "hr": 13, "barrel_pct": 3.3, "exit_velo": 78.8, "hr_fb_pct": 6.1, "iso": 0.142, "woba": 0.310, "player_id": 662139},
    {"name": "Josh Naylor",        "team": "CLE", "bats": "L", "hr": 13, "barrel_pct": 1.3, "exit_velo": 76.8, "hr_fb_pct": 4.7, "iso": 0.123, "woba": 0.330, "player_id": 647304},
    {"name": "Ketel Marte",        "team": "ARI", "bats": "S", "hr": 13, "barrel_pct": 3.0, "exit_velo": 78.5, "hr_fb_pct": 5.9, "iso": 0.140, "woba": 0.350, "player_id": 606466},
    {"name": "Michael Conforto",   "team": "SF",  "bats": "L", "hr": 13, "barrel_pct": 1.2, "exit_velo": 76.5, "hr_fb_pct": 4.5, "iso": 0.120, "woba": 0.310, "player_id": 624424},
    {"name": "Jurickson Profar",   "team": "SD",  "bats": "S", "hr": 13, "barrel_pct": 1.0, "exit_velo": 76.3, "hr_fb_pct": 4.3, "iso": 0.118, "woba": 0.340, "player_id": 595777},
    {"name": "Christian Yelich",   "team": "MIL", "bats": "L", "hr": 13, "barrel_pct": 2.8, "exit_velo": 78.2, "hr_fb_pct": 5.7, "iso": 0.138, "woba": 0.345, "player_id": 592885},
    {"name": "Randy Arozarena",    "team": "SEA", "bats": "R", "hr": 13, "barrel_pct": 2.6, "exit_velo": 78.0, "hr_fb_pct": 5.5, "iso": 0.135, "woba": 0.315, "player_id": 668227},
    {"name": "Nathaniel Lowe",     "team": "ARI", "bats": "L", "hr": 13, "barrel_pct": 2.4, "exit_velo": 77.7, "hr_fb_pct": 5.3, "iso": 0.132, "woba": 0.325, "player_id": 663993},
    {"name": "Cal Raleigh",        "team": "SEA", "bats": "S", "hr": 13, "barrel_pct": 4.0, "exit_velo": 79.2, "hr_fb_pct": 6.5, "iso": 0.150, "woba": 0.300, "player_id": 663728},
    {"name": "J.T. Realmuto",      "team": "PHI", "bats": "R", "hr": 13, "barrel_pct": 2.2, "exit_velo": 77.5, "hr_fb_pct": 5.1, "iso": 0.130, "woba": 0.315, "player_id": 592663},
    {"name": "Byron Buxton",       "team": "MIN", "bats": "R", "hr": 13, "barrel_pct": 4.5, "exit_velo": 80.0, "hr_fb_pct": 7.0, "iso": 0.158, "woba": 0.305, "player_id": 621439},
    {"name": "Corbin Carroll",     "team": "ARI", "bats": "L", "hr": 13, "barrel_pct": 2.0, "exit_velo": 77.2, "hr_fb_pct": 4.9, "iso": 0.128, "woba": 0.330, "player_id": 682998},
    {"name": "Anthony Volpe",      "team": "NYY", "bats": "R", "hr": 13, "barrel_pct": 1.8, "exit_velo": 77.0, "hr_fb_pct": 4.7, "iso": 0.125, "woba": 0.310, "player_id": 683011},
    {"name": "Jorge Soler",        "team": "ATL", "bats": "R", "hr": 13, "barrel_pct": 4.8, "exit_velo": 80.5, "hr_fb_pct": 7.3, "iso": 0.162, "woba": 0.295, "player_id": 624585},
    {"name": "Ezequiel Tovar",     "team": "COL", "bats": "R", "hr": 13, "barrel_pct": 1.6, "exit_velo": 76.8, "hr_fb_pct": 4.5, "iso": 0.122, "woba": 0.305, "player_id": 678545},
    {"name": "Joc Pederson",       "team": "TEX", "bats": "L", "hr": 13, "barrel_pct": 3.6, "exit_velo": 79.0, "hr_fb_pct": 6.2, "iso": 0.148, "woba": 0.310, "player_id": 592626},
    {"name": "Max Muncy",          "team": "LAD", "bats": "L", "hr": 13, "barrel_pct": 3.4, "exit_velo": 78.8, "hr_fb_pct": 6.0, "iso": 0.145, "woba": 0.335, "player_id": 571970},
    {"name": "Jesse Winker",       "team": "NYM", "bats": "L", "hr": 13, "barrel_pct": 1.4, "exit_velo": 76.5, "hr_fb_pct": 4.3, "iso": 0.120, "woba": 0.330, "player_id": 608385},
    {"name": "Andrew McCutchen",   "team": "KC",  "bats": "R", "hr": 13, "barrel_pct": 1.2, "exit_velo": 76.2, "hr_fb_pct": 4.1, "iso": 0.118, "woba": 0.315, "player_id": 457705},
    {"name": "Eugenio Suarez",     "team": "ARI", "bats": "R", "hr": 13, "barrel_pct": 3.2, "exit_velo": 78.5, "hr_fb_pct": 5.8, "iso": 0.142, "woba": 0.295, "player_id": 553993},
    {"name": "Josh Smith",         "team": "TEX", "bats": "L", "hr": 13, "barrel_pct": 1.0, "exit_velo": 76.0, "hr_fb_pct": 3.9, "iso": 0.115, "woba": 0.320, "player_id": 669701},
    {"name": "Cedric Mullins",     "team": "BAL", "bats": "L", "hr": 13, "barrel_pct": 2.8, "exit_velo": 78.2, "hr_fb_pct": 5.6, "iso": 0.138, "woba": 0.300, "player_id": 656775},
    {"name": "Riley Greene",       "team": "DET", "bats": "L", "hr": 13, "barrel_pct": 2.5, "exit_velo": 77.8, "hr_fb_pct": 5.3, "iso": 0.133, "woba": 0.325, "player_id": 682985},
    {"name": "Wenceel Perez",      "team": "DET", "bats": "S", "hr": 13, "barrel_pct": 1.1, "exit_velo": 76.1, "hr_fb_pct": 4.0, "iso": 0.116, "woba": 0.308, "player_id": 665862},
    {"name": "Jarren Duran",       "team": "BOS", "bats": "L", "hr": 13, "barrel_pct": 2.3, "exit_velo": 77.5, "hr_fb_pct": 5.1, "iso": 0.130, "woba": 0.340, "player_id": 680776},
]

# ──────────────────────────────────────────────────────────────────────────────
# Tier 3 — Longshots (Ranked 101-150, ~8-12 HR) — 9 pts per HR
# ──────────────────────────────────────────────────────────────────────────────
TIER_3_BATTERS = [
    {"name": "Maikel Garcia",      "team": "KC",  "bats": "R", "hr": 12, "barrel_pct": 2.0, "exit_velo": 76.5, "hr_fb_pct": 4.5, "iso": 0.115, "woba": 0.300, "player_id": 672580},
    {"name": "Ha-Seong Kim",       "team": "SD",  "bats": "R", "hr": 12, "barrel_pct": 1.8, "exit_velo": 76.2, "hr_fb_pct": 4.3, "iso": 0.112, "woba": 0.310, "player_id": 673490},
    {"name": "Taylor Ward",        "team": "LAA", "bats": "R", "hr": 12, "barrel_pct": 2.5, "exit_velo": 77.0, "hr_fb_pct": 5.0, "iso": 0.125, "woba": 0.315, "player_id": 621493},
    {"name": "Tyler Fitzgerald",   "team": "SF",  "bats": "R", "hr": 12, "barrel_pct": 3.0, "exit_velo": 77.5, "hr_fb_pct": 5.5, "iso": 0.135, "woba": 0.290, "player_id": 687590},
    {"name": "Mark Vientos",       "team": "NYM", "bats": "R", "hr": 12, "barrel_pct": 3.5, "exit_velo": 78.0, "hr_fb_pct": 6.0, "iso": 0.140, "woba": 0.305, "player_id": 668901},
    {"name": "Gavin Lux",          "team": "LAD", "bats": "L", "hr": 11, "barrel_pct": 1.6, "exit_velo": 76.0, "hr_fb_pct": 4.1, "iso": 0.110, "woba": 0.305, "player_id": 666158},
    {"name": "Patrick Wisdom",     "team": "CLE", "bats": "R", "hr": 11, "barrel_pct": 4.0, "exit_velo": 78.5, "hr_fb_pct": 6.5, "iso": 0.155, "woba": 0.275, "player_id": 621550},
    {"name": "Gio Urshela",        "team": "ATL", "bats": "R", "hr": 11, "barrel_pct": 1.4, "exit_velo": 75.8, "hr_fb_pct": 3.9, "iso": 0.108, "woba": 0.300, "player_id": 568245},
    {"name": "Paul Goldschmidt",   "team": "NYY", "bats": "R", "hr": 11, "barrel_pct": 2.2, "exit_velo": 76.8, "hr_fb_pct": 4.8, "iso": 0.120, "woba": 0.310, "player_id": 502671},
    {"name": "Whit Merrifield",    "team": "ATL", "bats": "R", "hr": 11, "barrel_pct": 1.2, "exit_velo": 75.5, "hr_fb_pct": 3.7, "iso": 0.105, "woba": 0.295, "player_id": 593160},
    {"name": "Willi Castro",       "team": "MIN", "bats": "S", "hr": 11, "barrel_pct": 1.9, "exit_velo": 76.3, "hr_fb_pct": 4.2, "iso": 0.113, "woba": 0.305, "player_id": 650489},
    {"name": "Nolan Jones",        "team": "COL", "bats": "L", "hr": 11, "barrel_pct": 3.2, "exit_velo": 77.8, "hr_fb_pct": 5.8, "iso": 0.138, "woba": 0.295, "player_id": 666134},
    {"name": "Joey Meneses",       "team": "WSH", "bats": "R", "hr": 10, "barrel_pct": 1.1, "exit_velo": 75.3, "hr_fb_pct": 3.5, "iso": 0.103, "woba": 0.290, "player_id": 608841},
    {"name": "Jon Berti",          "team": "MIA", "bats": "R", "hr": 10, "barrel_pct": 0.9, "exit_velo": 75.0, "hr_fb_pct": 3.3, "iso": 0.100, "woba": 0.305, "player_id": 542932},
    {"name": "Andrew Vaughn",      "team": "CWS", "bats": "R", "hr": 10, "barrel_pct": 2.8, "exit_velo": 77.2, "hr_fb_pct": 5.3, "iso": 0.132, "woba": 0.295, "player_id": 683734},
    {"name": "Jake Cronenworth",   "team": "SD",  "bats": "L", "hr": 10, "barrel_pct": 0.8, "exit_velo": 74.8, "hr_fb_pct": 3.2, "iso": 0.098, "woba": 0.315, "player_id": 630105},
    {"name": "Andres Gimenez",     "team": "CLE", "bats": "L", "hr": 10, "barrel_pct": 1.5, "exit_velo": 75.8, "hr_fb_pct": 3.8, "iso": 0.108, "woba": 0.310, "player_id": 665926},
    {"name": "Adam Frazier",       "team": "KC",  "bats": "L", "hr": 10, "barrel_pct": 0.7, "exit_velo": 74.5, "hr_fb_pct": 3.0, "iso": 0.095, "woba": 0.300, "player_id": 624428},
    {"name": "Yainer Diaz",        "team": "HOU", "bats": "R", "hr": 10, "barrel_pct": 2.5, "exit_velo": 77.0, "hr_fb_pct": 5.0, "iso": 0.128, "woba": 0.310, "player_id": 673237},
    {"name": "Spencer Steer",      "team": "CIN", "bats": "R", "hr": 10, "barrel_pct": 2.3, "exit_velo": 76.8, "hr_fb_pct": 4.8, "iso": 0.125, "woba": 0.305, "player_id": 668715},
    {"name": "Lane Thomas",        "team": "CLE", "bats": "R", "hr": 10, "barrel_pct": 2.0, "exit_velo": 76.5, "hr_fb_pct": 4.5, "iso": 0.120, "woba": 0.295, "player_id": 657041},
    {"name": "Brendan Rodgers",    "team": "COL", "bats": "R", "hr": 10, "barrel_pct": 1.3, "exit_velo": 75.5, "hr_fb_pct": 3.6, "iso": 0.105, "woba": 0.295, "player_id": 663898},
    {"name": "Alex Verdugo",       "team": "NYY", "bats": "L", "hr": 10, "barrel_pct": 1.0, "exit_velo": 75.0, "hr_fb_pct": 3.3, "iso": 0.100, "woba": 0.310, "player_id": 657077},
    {"name": "Nick Castellanos",   "team": "PHI", "bats": "R", "hr": 10, "barrel_pct": 2.0, "exit_velo": 76.3, "hr_fb_pct": 4.3, "iso": 0.118, "woba": 0.305, "player_id": 592206},
    {"name": "Brandon Marsh",      "team": "PHI", "bats": "L", "hr": 9, "barrel_pct": 1.8, "exit_velo": 76.0, "hr_fb_pct": 4.0, "iso": 0.113, "woba": 0.315, "player_id": 669016},
    {"name": "Jake McCarthy",      "team": "ARI", "bats": "L", "hr": 9, "barrel_pct": 1.5, "exit_velo": 75.5, "hr_fb_pct": 3.7, "iso": 0.108, "woba": 0.298, "player_id": 664983},
    {"name": "Jose Iglesias",      "team": "NYM", "bats": "R", "hr": 9, "barrel_pct": 0.6, "exit_velo": 74.0, "hr_fb_pct": 2.8, "iso": 0.090, "woba": 0.320, "player_id": 578428},
    {"name": "Luis Rengifo",       "team": "LAA", "bats": "S", "hr": 9, "barrel_pct": 1.2, "exit_velo": 75.2, "hr_fb_pct": 3.5, "iso": 0.105, "woba": 0.305, "player_id": 650859},
    {"name": "Isiah Kiner-Falefa", "team": "PIT", "bats": "R", "hr": 9, "barrel_pct": 0.5, "exit_velo": 73.8, "hr_fb_pct": 2.6, "iso": 0.088, "woba": 0.295, "player_id": 643396},
    {"name": "Tommy Pham",         "team": "STL", "bats": "R", "hr": 9, "barrel_pct": 2.2, "exit_velo": 76.5, "hr_fb_pct": 4.5, "iso": 0.120, "woba": 0.290, "player_id": 502054},
    {"name": "Enrique Hernandez",  "team": "LAD", "bats": "R", "hr": 9, "barrel_pct": 1.0, "exit_velo": 75.0, "hr_fb_pct": 3.2, "iso": 0.098, "woba": 0.295, "player_id": 571771},
    {"name": "Yoan Moncada",       "team": "CLE", "bats": "S", "hr": 9, "barrel_pct": 1.8, "exit_velo": 76.0, "hr_fb_pct": 4.0, "iso": 0.112, "woba": 0.300, "player_id": 660162},
    {"name": "Dylan Carlson",      "team": "STL", "bats": "S", "hr": 9, "barrel_pct": 1.5, "exit_velo": 75.5, "hr_fb_pct": 3.7, "iso": 0.105, "woba": 0.298, "player_id": 666185},
    {"name": "Myles Straw",        "team": "CLE", "bats": "R", "hr": 9, "barrel_pct": 0.4, "exit_velo": 73.5, "hr_fb_pct": 2.4, "iso": 0.085, "woba": 0.288, "player_id": 664702},
    {"name": "Zach Neto",          "team": "LAA", "bats": "R", "hr": 9, "barrel_pct": 2.5, "exit_velo": 77.0, "hr_fb_pct": 5.0, "iso": 0.125, "woba": 0.300, "player_id": 700825},
    {"name": "Justin Turner",      "team": "SEA", "bats": "R", "hr": 8, "barrel_pct": 0.8, "exit_velo": 74.5, "hr_fb_pct": 3.0, "iso": 0.095, "woba": 0.305, "player_id": 457759},
    {"name": "Keibert Ruiz",       "team": "WSH", "bats": "S", "hr": 8, "barrel_pct": 1.3, "exit_velo": 75.2, "hr_fb_pct": 3.5, "iso": 0.102, "woba": 0.300, "player_id": 660688},
    {"name": "Nick Ahmed",         "team": "MIN", "bats": "R", "hr": 8, "barrel_pct": 0.3, "exit_velo": 73.2, "hr_fb_pct": 2.2, "iso": 0.082, "woba": 0.280, "player_id": 605113},
    {"name": "Jake Bauers",        "team": "MIL", "bats": "L", "hr": 8, "barrel_pct": 1.6, "exit_velo": 75.8, "hr_fb_pct": 3.8, "iso": 0.108, "woba": 0.295, "player_id": 641343},
    {"name": "Thairo Estrada",     "team": "SF",  "bats": "R", "hr": 8, "barrel_pct": 0.6, "exit_velo": 74.0, "hr_fb_pct": 2.8, "iso": 0.090, "woba": 0.300, "player_id": 642731},
    {"name": "Brandon Lowe",       "team": "TB",  "bats": "L", "hr": 8, "barrel_pct": 2.0, "exit_velo": 76.3, "hr_fb_pct": 4.3, "iso": 0.118, "woba": 0.295, "player_id": 664040},
    {"name": "Jonathan India",     "team": "CIN", "bats": "R", "hr": 8, "barrel_pct": 1.4, "exit_velo": 75.5, "hr_fb_pct": 3.6, "iso": 0.105, "woba": 0.310, "player_id": 663697},
    {"name": "Garrett Cooper",     "team": "WSH", "bats": "R", "hr": 8, "barrel_pct": 1.1, "exit_velo": 75.0, "hr_fb_pct": 3.3, "iso": 0.098, "woba": 0.300, "player_id": 643265},
    {"name": "Adam Duvall",        "team": "ATL", "bats": "R", "hr": 8, "barrel_pct": 3.0, "exit_velo": 77.5, "hr_fb_pct": 5.5, "iso": 0.135, "woba": 0.275, "player_id": 594807},
    {"name": "Harrison Bader",     "team": "SF",  "bats": "R", "hr": 8, "barrel_pct": 0.9, "exit_velo": 74.8, "hr_fb_pct": 3.1, "iso": 0.095, "woba": 0.290, "player_id": 664056},
    {"name": "Leody Taveras",      "team": "TEX", "bats": "S", "hr": 8, "barrel_pct": 0.7, "exit_velo": 74.3, "hr_fb_pct": 2.8, "iso": 0.090, "woba": 0.285, "player_id": 665750},
    {"name": "Luis Garcia Jr.",    "team": "WSH", "bats": "L", "hr": 8, "barrel_pct": 0.5, "exit_velo": 74.0, "hr_fb_pct": 2.5, "iso": 0.085, "woba": 0.295, "player_id": 671277},
    {"name": "Kevin Newman",       "team": "ARI", "bats": "R", "hr": 8, "barrel_pct": 0.4, "exit_velo": 73.5, "hr_fb_pct": 2.3, "iso": 0.082, "woba": 0.288, "player_id": 621028},
    {"name": "Chas McCormick",     "team": "HOU", "bats": "R", "hr": 8, "barrel_pct": 2.3, "exit_velo": 76.8, "hr_fb_pct": 4.8, "iso": 0.122, "woba": 0.295, "player_id": 676801},
    {"name": "Manuel Margot",      "team": "MIN", "bats": "R", "hr": 8, "barrel_pct": 0.6, "exit_velo": 74.2, "hr_fb_pct": 2.7, "iso": 0.088, "woba": 0.290, "player_id": 622534},
]


# ──────────────────────────────────────────────────────────────────────────────
# Pitchers (33 — mix of HR-prone and aces for matchup scoring)
# ──────────────────────────────────────────────────────────────────────────────
PITCHERS_2025 = [
    {"name": "Gerrit Cole",       "hr_per_9": 0.8, "era": 2.85, "hard_hit_pct_allowed": 30, "throws": "R", "k_per_9": 10.2},
    {"name": "Spencer Strider",   "hr_per_9": 0.9, "era": 3.10, "hard_hit_pct_allowed": 31, "throws": "R", "k_per_9": 11.5},
    {"name": "Zack Wheeler",      "hr_per_9": 0.7, "era": 2.75, "hard_hit_pct_allowed": 29, "throws": "R", "k_per_9": 9.8},
    {"name": "Corbin Burnes",     "hr_per_9": 0.8, "era": 2.90, "hard_hit_pct_allowed": 30, "throws": "R", "k_per_9": 9.5},
    {"name": "Blake Snell",       "hr_per_9": 1.1, "era": 3.50, "hard_hit_pct_allowed": 33, "throws": "L", "k_per_9": 10.8},
    {"name": "Tyler Glasnow",     "hr_per_9": 1.0, "era": 3.20, "hard_hit_pct_allowed": 32, "throws": "R", "k_per_9": 11.2},
    {"name": "Logan Webb",        "hr_per_9": 0.6, "era": 2.65, "hard_hit_pct_allowed": 28, "throws": "R", "k_per_9": 8.5},
    {"name": "Framber Valdez",    "hr_per_9": 0.7, "era": 2.80, "hard_hit_pct_allowed": 29, "throws": "L", "k_per_9": 8.0},
    {"name": "Shane McClanahan",  "hr_per_9": 0.9, "era": 3.15, "hard_hit_pct_allowed": 31, "throws": "L", "k_per_9": 10.0},
    {"name": "Chris Sale",        "hr_per_9": 1.2, "era": 3.60, "hard_hit_pct_allowed": 34, "throws": "L", "k_per_9": 10.5},
    {"name": "Max Fried",         "hr_per_9": 0.6, "era": 2.60, "hard_hit_pct_allowed": 28, "throws": "L", "k_per_9": 8.8},
    {"name": "Yoshinobu Yamamoto","hr_per_9": 0.8, "era": 3.00, "hard_hit_pct_allowed": 30, "throws": "R", "k_per_9": 9.2},
    {"name": "Pablo Lopez",       "hr_per_9": 1.0, "era": 3.40, "hard_hit_pct_allowed": 32, "throws": "R", "k_per_9": 9.0},
    {"name": "Justin Verlander",  "hr_per_9": 1.3, "era": 3.80, "hard_hit_pct_allowed": 35, "throws": "R", "k_per_9": 8.5},
    {"name": "Kevin Gausman",     "hr_per_9": 1.1, "era": 3.45, "hard_hit_pct_allowed": 33, "throws": "R", "k_per_9": 9.3},
    {"name": "Tarik Skubal",      "hr_per_9": 0.7, "era": 2.70, "hard_hit_pct_allowed": 29, "throws": "L", "k_per_9": 10.5},
    {"name": "Sonny Gray",        "hr_per_9": 1.0, "era": 3.30, "hard_hit_pct_allowed": 32, "throws": "R", "k_per_9": 9.0},
    {"name": "Luis Castillo",     "hr_per_9": 1.1, "era": 3.55, "hard_hit_pct_allowed": 33, "throws": "R", "k_per_9": 9.5},
    {"name": "Aaron Nola",        "hr_per_9": 1.2, "era": 3.65, "hard_hit_pct_allowed": 34, "throws": "R", "k_per_9": 9.2},
    {"name": "Marcus Stroman",    "hr_per_9": 0.9, "era": 3.25, "hard_hit_pct_allowed": 31, "throws": "R", "k_per_9": 7.5},
    {"name": "Nestor Cortes",     "hr_per_9": 1.0, "era": 3.35, "hard_hit_pct_allowed": 32, "throws": "L", "k_per_9": 8.5},
    {"name": "Joe Ryan",          "hr_per_9": 1.4, "era": 4.00, "hard_hit_pct_allowed": 36, "throws": "R", "k_per_9": 9.8},
    {"name": "Bailey Ober",       "hr_per_9": 1.3, "era": 3.75, "hard_hit_pct_allowed": 35, "throws": "R", "k_per_9": 9.5},
    {"name": "Carlos Rodon",      "hr_per_9": 1.5, "era": 4.20, "hard_hit_pct_allowed": 37, "throws": "L", "k_per_9": 10.0},
    {"name": "Patrick Corbin",    "hr_per_9": 1.8, "era": 5.10, "hard_hit_pct_allowed": 40, "throws": "L", "k_per_9": 7.0},
    {"name": "Jordan Lyles",      "hr_per_9": 1.7, "era": 4.80, "hard_hit_pct_allowed": 39, "throws": "R", "k_per_9": 6.8},
    {"name": "Lance Lynn",        "hr_per_9": 1.6, "era": 4.50, "hard_hit_pct_allowed": 38, "throws": "R", "k_per_9": 8.2},
    {"name": "Michael Wacha",     "hr_per_9": 1.3, "era": 3.70, "hard_hit_pct_allowed": 35, "throws": "R", "k_per_9": 8.0},
    {"name": "Kyle Hendricks",    "hr_per_9": 1.5, "era": 4.30, "hard_hit_pct_allowed": 37, "throws": "R", "k_per_9": 6.5},
    {"name": "Robbie Ray",        "hr_per_9": 1.6, "era": 4.60, "hard_hit_pct_allowed": 38, "throws": "L", "k_per_9": 10.5},
    {"name": "Eduardo Rodriguez", "hr_per_9": 1.2, "era": 3.60, "hard_hit_pct_allowed": 34, "throws": "L", "k_per_9": 8.8},
    {"name": "JP Sears",          "hr_per_9": 1.4, "era": 4.10, "hard_hit_pct_allowed": 36, "throws": "L", "k_per_9": 7.8},
    {"name": "Mitch Keller",      "hr_per_9": 1.1, "era": 3.50, "hard_hit_pct_allowed": 33, "throws": "R", "k_per_9": 8.5},
]


# ──────────────────────────────────────────────────────────────────────────────
# Assembled tier dict
# ──────────────────────────────────────────────────────────────────────────────
ALL_TIERS = {
    1: TIER_1_BATTERS,
    2: TIER_2_BATTERS,
    3: TIER_3_BATTERS,
}


# ──────────────────────────────────────────────────────────────────────────────
# Helper functions
# ──────────────────────────────────────────────────────────────────────────────
def get_tier_for_batter(name: str) -> int | None:
    """Return tier (1/2/3) for a batter name, or None if not found."""
    for tier, batters in ALL_TIERS.items():
        for b in batters:
            if b["name"] == name:
                return tier
    return None


def get_all_batters_lookup() -> dict:
    """Return {name: batter_dict} for all batters across all tiers."""
    lookup = {}
    for batters in ALL_TIERS.values():
        for b in batters:
            lookup[b["name"]] = b
    return lookup
