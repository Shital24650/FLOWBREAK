import {describe,it,expect} from "vitest";
describe("FLOWBREAK frontend",()=>{it("runs in a DOM environment",()=>{expect(document.createElement("main").tagName).toBe("MAIN");});});
