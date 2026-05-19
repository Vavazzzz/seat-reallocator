OCCUPIED = {'CONFIRMED', 'RESALE'}
VALID    = {'CONFIRMED', 'RESALE', 'CANCELLED'}

MAX_BRANCHES = 25   # candidate placements tried per prob order during backtracking

ILP_TIME_LIMIT     = 10        # seconds — per-segment CBC budget
BT_TIME_LIMIT      = 1.0       # seconds — per-segment backtracking budget
INFEASIBLE_PENALTY = 1_000_000 # ILP cost for leaving a prob order non-adjacent
COLL_PENALTY       = 10_000    # ILP cost for displacing an already-adjacent non-prob order
