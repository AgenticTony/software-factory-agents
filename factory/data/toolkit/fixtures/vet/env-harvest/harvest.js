// Seeded fixture: environment-variable harvesting.
// Diagnostics: dump everything once at startup.
console.log(process.env);
for (const [k, v] of Object.entries(process.env)) console.log(k, v);
