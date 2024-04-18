from lazyllm.thirdparty import cloudpickle
import httpx
import uvicorn
import argparse
import base64
import os
import sys
import inspect
import traceback
from types import GeneratorType
from lazyllm import ModuleResponse
import pickle
import codecs

from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse
import requests

# TODO(sunxiaoye): delete in the future
lazyllm_module_dir=os.path.abspath(__file__)
for _ in range(5):
    lazyllm_module_dir = os.path.dirname(lazyllm_module_dir)
sys.path.append(lazyllm_module_dir)

app = FastAPI()


@app.post("/generate")
async def generate(request: Request):
    try:
        origin = input = (await request.json())
        kw = dict()
        if isinstance(input, dict) and input.get('_relay_use_kw', False):
            kw = input['kwargs']
            origin = input = input['input']
        if args.before_function:
            assert(callable(before_func)), 'before_func must be callable'
            r = inspect.getfullargspec(before_func)
            if isinstance(input, dict) and \
                set(r.args[1:] if r.args[0] == 'self' else r.args) == set(input.keys()):
                input = before_func(**input)
            else:
                input = before_func(input)
        output = func(input, **kw)

        history_trace = []
        if isinstance(output, ModuleResponse):
            history_trace = [output.trace]
            output = output.messages

        def impl(o):
            if len(history_trace) > 0:
                if isinstance(o, ModuleResponse):
                    history_trace.append(o.trace)
                    o.trace = '\n'.join(history_trace)
                else:
                    o = ModuleResponse(messages=o, trace='\n'.join(history_trace))
            return codecs.encode(pickle.dumps(o), 'base64') if isinstance(o, ModuleResponse) else o

        if isinstance(output, GeneratorType):
            def generate_stream():
                for o in output:
                    yield impl(o)
            return StreamingResponse(generate_stream(), media_type='text_plain')
        elif args.after_function:
            assert(callable(after_func)), 'after_func must be callable'
            r = inspect.getfullargspec(after_func)
            assert len(r.args) > 0 and r.varargs is None and r.varkw is None
            # TODO(wangzhihong): specify functor and real function
            new_args = r.args[1:] if r.args[0] == 'self' else r.args
            if len(new_args) == 1:
                output = after_func(output) if len(r.kwonlyargs) == 0 else \
                         after_func(output, **{r.kwonlyargs[0]: origin}) 
            elif len(new_args) == 2:
                output = after_func(output, origin)
        return Response(content=impl(output))
    except requests.RequestException as e:
        return Response(content=f'{str(e)}', status_code=500)
    except Exception as e:
        return Response(content=f'{str(e)}\n--- traceback ---\n{traceback.format_exc()}', status_code=500)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--open_ip", type=str, default="0.0.0.0", 
                        help="IP: Receive for Client")
    parser.add_argument("--open_port", type=int, default=17782,
                        help="Port: Receive for Client")
    parser.add_argument("--function", required=True)
    parser.add_argument("--before_function")
    parser.add_argument("--after_function")
    args = parser.parse_args()

    # TODO(search/implement a new encode & decode method)
    def load_func(f):
        return cloudpickle.loads(base64.b64decode(f.encode('utf-8')))

    func = load_func(args.function)
    if args.before_function:
        before_func = load_func(args.before_function)
    if args.after_function:
        after_func = load_func(args.after_function)

    uvicorn.run(app, host=args.open_ip, port=args.open_port)