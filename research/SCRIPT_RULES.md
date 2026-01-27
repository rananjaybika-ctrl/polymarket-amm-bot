# Script Development Rules

## MANDATORY for all backtest/simulation scripts:

1. **PROGRESS BAR** - Use tqdm or print progress every N iterations
   ```python
   from tqdm import tqdm
   for i, config in tqdm(enumerate(configs), total=len(configs), desc="Running configs"):
   ```

2. **ETA ESTIMATE** - Show estimated time remaining

3. **CHECKPOINT SAVES** - Save partial results every N configs so crashes don't lose everything

4. **CONFIG COUNT** - Print "Running X of Y" at minimum

## Example template:
```python
from tqdm import tqdm
import time

start = time.time()
results = []

for i, config in tqdm(enumerate(configs), total=len(configs)):
    result = run_config(config)
    results.append(result)

    # Checkpoint every 10 configs
    if (i + 1) % 10 == 0:
        pd.DataFrame(results).to_csv("checkpoint.csv", index=False)

# Final save
pd.DataFrame(results).to_csv("final_results.csv", index=False)
print(f"Completed in {(time.time() - start) / 60:.1f} minutes")
```
