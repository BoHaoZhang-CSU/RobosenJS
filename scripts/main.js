"use strict";

const { K1 } = require("../");
const program = require("../scripts/program");

(async function main() {
  const k1 = new K1();
  await k1.on();
  await program.main(k1);
  await k1.end();
})();
