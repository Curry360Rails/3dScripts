"""Read-only AnyLogic Design-Time API railway hierarchy diagnostic."""
from anylogic_design_time_api import Workspace

TOKEN = 'd68e12ab-4988-447b-80b1-fe92bc82fc77'
MODEL_NAME = 'CEMEX_Victorville'
AGENT_NAME = 'Main'


def describe(element, indent=''):
    try:
        name = element.getName()
    except Exception as exc:
        name = f'<name error: {exc}>'
    try:
        raw_type = element.item.typeName()
    except Exception as exc:
        raw_type = f'<type error: {exc}>'
    print(f'{indent}{name!s} | wrapper={type(element).__name__} | rawType={raw_type}')


def children_of(element, indent=''):
    try:
        children = element.getChildren()
    except Exception as exc:
        print(f'{indent}getChildren FAILED: {str(exc).splitlines()[0]}')
        return []
    print(f'{indent}children={len(children)}')
    return children


def main():
    ws = Workspace(TOKEN)
    agent = ws.getModel(MODEL_NAME).getAgent(AGENT_NAME)
    levels = agent.getLevels()
    print('Agent:', agent.getName(), '| getLevels count:', len(levels))
    for i, level in enumerate(levels):
        print(f'\nLEVEL {i}:')
        describe(level, '  ')
        for child in children_of(level, '  '):
            describe(child, '    ')
            # Only descend into known container type. Never call getChildren on a switch/track.
            if child.item.typeName() == 'RailwayNetwork':
                for member in children_of(child, '      '):
                    describe(member, '        ')
    print('\nNamed lookup from agent:')
    for name in ('level', 'railwayNetwork'):
        try:
            item = agent.getLevel(name)
            if item is not None:
                describe(item, '  ')
                if name == 'level':
                    for child in children_of(item, '    '):
                        describe(child, '      ')
        except Exception as exc:
            print(f'  {name}: {str(exc).splitlines()[0]}')

if __name__ == '__main__':
    main()
