---
allowed_sections: ["X"]
length_budget: 50
---

## X

A deliberately-malformed template shape-spec: it declares an allowed-sections list and a budget but NO
required-sections list, which template.v1 requires. The standing template-shape-spec check must catch this.
