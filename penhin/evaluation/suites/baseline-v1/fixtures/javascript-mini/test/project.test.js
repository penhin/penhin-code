import test from "node:test";
import assert from "node:assert/strict";
import { add, subtract } from "../src/math.js";
import { normalize, wordCount } from "../src/text.js";
import { Inventory } from "../src/inventory.js";

test("public behavior", () => {
  assert.equal(add(2, 3), 5);
  assert.equal(subtract(7, 2), 5);
  assert.equal(normalize("  Hello  "), "hello");
  assert.equal(wordCount(" two   words "), 2);
  const inventory = new Inventory();
  inventory.add("A", 3);
  assert.equal(inventory.quantity("A"), 3);
});
