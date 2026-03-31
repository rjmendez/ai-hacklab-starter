# Dispatch System Overview

The Dispatch layer orchestrates routing tasks to the appropriate agents within the mesh infrastructure. Key features include:

1. **Key Pools:** Ensures keys and budget limits are strictly adhered to. Uses a weighted pool strategy.
2. **Tier System:** Ranks tasks into cost efficiency (free → nano → cheap → premium).
3. **Circuit Breakers:** Temporary disables a pool when thresholds are exceeded.

For detailed operation, see `docs/model-routing.md`.