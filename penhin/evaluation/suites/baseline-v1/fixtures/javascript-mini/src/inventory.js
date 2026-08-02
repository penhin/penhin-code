export class Inventory {
  constructor() {
    this.stock = new Map();
  }

  add(sku, quantity) {
    if (quantity < 0) throw new RangeError("quantity must be non-negative");
    this.stock.set(sku, (this.stock.get(sku) ?? 0) + quantity);
  }

  quantity(sku) {
    return this.stock.get(sku) ?? 0;
  }
}
