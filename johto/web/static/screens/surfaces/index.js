// Surface registry: item.surface -> component. Each surface file owns its component.
import write_prompt from "./write_prompt.js";
import details from "./details.js";
import decide from "./decide.js";
import rate from "./rate.js";
import studio from "./studio.js";
import pick from "./pick.js";
import lane from "./lane.js";
import blocker from "./blocker.js";
import log_discard from "./log_discard.js";
import feedback from "./feedback.js";

export const SURFACES = { write_prompt, details, decide, rate, studio, pick, lane, blocker, log_discard, feedback };
