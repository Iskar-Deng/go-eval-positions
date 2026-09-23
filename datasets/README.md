# Dataset format

Each accepted sample is stored in `accepted/<sample-id>/`:

```text
sample.json
X.sgf
Y.sgf
```

- `X.sgf`: original position with full move history.
- `Y.sgf`: perturbed position with full move history.
- `sample.json`: shared candidate moves and KataGo analysis.

All samples use Chinese rules and 7.5 komi. `A`, `B`, and `C` are identical in X and Y. Only X/A creates atari; every other position–move combination does not.

```json
{
  "source_sgf": "outputs/games/.../annotated.sgf",
  "move_number": 58,
  "moves": {
    "A": "A10",
    "B": "N4",
    "C": "G3"
  },
  "results": {
    "X": {
      "winrate": {"A": 0.55, "B": 0.57, "C": 0.10},
      "score_lead": {"A": 1.28, "B": 1.14, "C": -4.77}
    },
    "Y": {
      "winrate": {"A": 0.01, "B": 0.45, "C": 0.07},
      "score_lead": {"A": -12.75, "B": -0.02, "C": -5.76}
    }
  },
  "X_A_targets": [
    {"size": 4, "stones": ["B10", "C10", "C11", "C9"]}
  ]
}
```

- `move_number`: number of moves already played in the position.
- `winrate`: KataGo win probability for the player to move.
- `score_lead`: KataGo score lead from the player-to-move perspective.
- `X_A_targets`: groups placed in atari by playing A in X, including size and stone coordinates.
