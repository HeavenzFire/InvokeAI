import { getCollectItemType } from 'features/nodes/store/util/getCollectItemType';
import { add, buildEdge, buildNode, collect, templates } from 'features/nodes/store/util/testUtils';
import type { FieldType } from 'features/nodes/types/field';
import { describe, expect, it } from 'vitest';

describe(getCollectItemType.name, () => {
  it('should return the type of the items the collect node collects', () => {
    const n1 = buildNode(add);
    const n2 = buildNode(collect);
    const nodes = [n1, n2];
    const edges = [buildEdge(n1.id, 'value', n2.id, 'item')];
    const result = getCollectItemType(templates, nodes, edges, n2.id);
    expect(result).toEqual<FieldType>({ name: 'IntegerField', isCollection: false, isCollectionOrScalar: false });
  });
  it('should return null if the collect node does not have any connections', () => {
    const n1 = buildNode(collect);
    const nodes = [n1];
    const result = getCollectItemType(templates, nodes, [], n1.id);
    expect(result).toBeNull();
  });
});
